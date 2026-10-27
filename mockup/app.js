// OTT Arena web client — giao diện kiểu Lichess, gọi API thật tại /api/v1.
import { createNet } from './net.js';
const CFG = { mode: 'local', pollMs: 10000, ...(window.OTT_CONFIG || {}) };
const TUNNEL = CFG.mode === 'tunnel'; // Quick Tunnel: không SSE, polling thưa, có backoff
const NET = createNet({ tunnel: TUNNEL, pollMs: CFG.pollMs });
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* ================= API ================= */
const S = { me: null, csrf: null, timers: [], serverOffset: 0 };
class ApiError extends Error { constructor(status, code, message) { super(message); this.status = status; this.code = code; } }
async function api(method, path, body) {
  let res; const failureEpoch = NET.state.failureEpoch;
  try {
    res = await fetch('/api/v1' + path, {
      method, credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', ...(S.csrf && method !== 'GET' ? { 'X-CSRF-Token': S.csrf } : {}) },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (e) { NET.noteFailure(); throw e; }
  if ([429, 502, 503, 504].includes(res.status)) NET.noteFailure(parseInt(res.headers.get('Retry-After'), 10) || 0); else NET.noteOk(failureEpoch);
  const ctype = res.headers.get('Content-Type') || '';
  const data = res.status === 204 ? null : ctype.includes('json') ? await res.json() : await res.text();
  if (!res.ok) throw new ApiError(res.status, data?.error?.code || 'ERROR', data?.error?.message || `Lỗi ${res.status}`);
  return data;
}
async function loadMe() {
  try { const me = await api('GET', '/me'); S.me = { username: me.username, role: me.role }; S.csrf = me.csrf_token; }
  catch { S.me = null; S.csrf = null; }
}
const isOrg = () => S.me?.role === 'ORGANIZER';
const isContestant = () => S.me?.role === 'CONTESTANT';

/* ================= Hằng số & tiện ích ================= */
const FILES = 'abcdefghi';
const P2L = { scissors: 'k', hammer: 'b', paper: 'a', king: 'v' };
const LET = { k: 'K', b: 'B', a: 'A', v: 'V' };
const PNAME = { k: 'Kéo', b: 'Búa', a: 'Bao', v: 'Vua' };
const STATUS = {
  DRAFT: ['Nháp', ''], REGISTRATION_OPEN: ['Đang mở đăng ký', 'info'], CODING: ['Đang viết bot', 'warn'], LOCKED: ['Đã khóa', 'warn'],
  RUNNING: ['Đang thi đấu', 'live'], FINISHED: ['Đã kết thúc', ''], CANCELLED: ['Đã hủy', 'err'],
};
const REASONS = {
  KING_CAPTURED: 'bắt Vua', ALL_CAPTURED: 'loại hết quân', NO_LEGAL_MOVES: 'hết nước đi hợp lệ', TIMEOUT: 'hết giờ', INVALID_ACTION: 'action sai định dạng',
  ILLEGAL_ACTION: 'nước đi không hợp lệ', BOT_CRASH: 'bot crash', EXTRA_OUTPUT: 'bot in thừa dòng', OUTPUT_TOO_LARGE: 'output quá lớn', BOT_START_FAILED: 'bot không chạy được',
  REPETITION: 'lặp thế cờ 3 lần', MOVE_LIMIT: 'đủ 500 nước',
};
const SCORE = { WHITE_WIN: '1–0', BLACK_WIN: '0–1', DRAW: '½–½' };
const tag = (t) => { const [label, cls] = STATUS[t] || [t, '']; return `<span class="tag ${cls}">${label}</span>`; };
const fmtClock = (ms) => { const s = Math.max(0, Math.ceil(ms / 1000)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; };
const fmtTime = (ts) => ts ? new Date(ts * 1000).toLocaleTimeString('vi-VN') : '—';
const fmtDate = (ts) => ts ? new Date(ts * 1000).toLocaleString('vi-VN') : '—';
const nowSrv = () => Date.now() / 1000 + S.serverOffset;
const fmtCountdown = (sec) => {
  sec = Math.max(0, Math.ceil(sec));
  return `${String(Math.floor(sec / 60)).padStart(2, '0')}:${String(sec % 60).padStart(2, '0')}`;
};
const clockLabel = (secs) => `${secs / 60}+0`;
function syncOffset(serverTime) { if (serverTime) S.serverOffset = serverTime - Date.now() / 1000; }
function every(ms, fn) { const id = setInterval(fn, ms); S.timers.push(id); return id; }
function clearTimers() {
  S.timers.forEach(clearInterval); S.timers = []; NET.stopAll();
  clearTimeout(C.saveTimer); C.saveTimer = null;
  if (C.dirty || C.savePromise) saveDraft(C);
}
// Poll mạng: dùng cho mọi lần gọi API định kỳ (khác every() chỉ dành cho đồng hồ cục bộ). Trả handle cho stopPoll().
const poll = (ms, fn) => NET.poll(ms, fn);
let toastT;
function toast(msg, bad = false) {
  const t = $('#toast'); t.textContent = msg; t.style.borderColor = bad ? '#c33' : ''; t.classList.add('on');
  clearTimeout(toastT); toastT = setTimeout(() => t.classList.remove('on'), 3200);
}
const errMsg = (e) => (e instanceof ApiError ? e.message : 'Không kết nối được máy chủ.');

/* ================= Bàn cờ ================= */
function fromServerBoard(b) {
  const out = {};
  for (const [sq, p] of Object.entries(b || {})) out[sq] = { s: p.side === 'white' ? 'w' : 'b', p: P2L[p.piece], pr: p.promotion };
  return out;
}
function buildPositions(m) {
  const start = {};
  for (const p of m.placements || []) start[p.to] = { s: p.side === 'white' ? 'w' : 'b', p: P2L[p.piece], pr: null };
  const positions = [start];
  let b = start;
  for (const mv of m.moves || []) {
    const nb = { ...b }, pc = { ...nb[mv.from] };
    delete nb[mv.from];
    if (mv.promotion) pc.pr = mv.promotion;
    nb[mv.to] = pc; positions.push(nb); b = nb;
  }
  return positions;
}
function samePlacement(a, b) { return a.side === b.side && a.piece === b.piece && a.to === b.to; }
function sameMove(a, b) {
  return a.n === b.n && a.side === b.side && a.from === b.from && a.to === b.to && a.piece === b.piece && a.promotion === b.promotion && a.captured === b.captured;
}
function matchPositions(m) {
  const previous = G.m;
  const placements = m.placements || [], oldPlacements = previous?.placements || [];
  if (!previous || previous.id !== m.id || placements.length !== oldPlacements.length || placements.some((p, i) => !samePlacement(p, oldPlacements[i]))) return { positions: buildPositions(m), changed: true };

  const moves = m.moves || [], oldMoves = previous.moves || [];
  let common = 0;
  while (common < moves.length && common < oldMoves.length && sameMove(moves[common], oldMoves[common])) common++;
  if (common === moves.length && common === oldMoves.length) return { positions: G.positions, changed: false };
  if (common === oldMoves.length && G.positions.length === oldMoves.length + 1) {
    let board = G.positions[G.positions.length - 1];
    for (; common < moves.length; common++) {
      const move = moves[common], next = { ...board }, piece = { ...next[move.from] };
      delete next[move.from];
      if (move.promotion) piece.pr = move.promotion;
      next[move.to] = piece;
      G.positions.push(next);
      board = next;
    }
    return { positions: G.positions, changed: true };
  }
  if (common === moves.length) {
    G.positions.length = common + 1;
    return { positions: G.positions, changed: true };
  }
  return { positions: buildPositions(m), changed: true };
}
function pieceSVG(pc) { return `<svg class="piece" viewBox="0 0 100 100"><title>${PNAME[pc.p]}</title><use href="#pc-${pc.s}${pc.p}"/></svg>`; }
function boardHTML(pos, { flip = false, last = null, coords = true } = {}) {
  const ranks = [...Array(9).keys()];
  const rs = flip ? ranks : ranks.reverse();
  const fs = flip ? [...Array(9).keys()].reverse() : [...Array(9).keys()];
  let h = '<div class="cg-board">';
  for (const r of rs) for (const f of fs) {
    const s = FILES[f] + (r + 1), pc = pos[s];
    const cls = ['sq', (f + r) % 2 === 0 ? 'dark' : 'light'];
    if (last && (last.from === s || last.to === s)) cls.push('last');
    let inner = '';
    if (coords && (flip ? f === 0 : f === 8)) inner += `<span class="coord rank">${r + 1}</span>`;
    if (coords && (flip ? r === 8 : r === 0)) inner += `<span class="coord file">${FILES[f]}</span>`;
    if (pc) { inner += pieceSVG(pc); if (pc.pr) inner += `<span class="promo" title="${pc.pr === 'rook' ? 'Xe' : 'Tượng'}">${pc.pr === 'rook' ? 'X' : 'T'}</span>`; }
    h += `<div class="${cls.join(' ')}">${inner}</div>`;
  }
  return h + '</div>';
}
function capturedBy(positions, ply, victimSide) {
  const cnt = { k: 3, b: 3, a: 3, v: 1 };
  Object.values(positions[ply] || {}).forEach((p) => { if (p.s === victimSide) cnt[p.p]--; });
  const total = cnt.k + cnt.b + cnt.a + cnt.v;
  if (!total || !Object.keys(positions[0]).length) return '';
  const icons = ['v', 'k', 'b', 'a'].flatMap((t) => Array(Math.max(0, cnt[t])).fill(`<svg viewBox="0 0 100 100"><use href="#pc-${victimSide}${t}"/></svg>`)).join('');
  return `<span class="captured">${icons}<b>+${total}</b></span>`;
}
const san = (mv) => `${LET[P2L[mv.piece]]}${mv.from}${mv.captured ? '×' : '–'}${mv.to}${mv.promotion ? (mv.promotion === 'rook' ? '=X' : '=T') : ''}`;

/* ================= Thành phần chung ================= */
const ICON_KNIGHT = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8.959 1.99l-.147 .028l-.115 .029a1 1 0 0 0 -.646 1.27l.749 2.245l-2.815 1.735a2 2 0 0 0 -.655 2.751l.089 .133a2 2 0 0 0 1.614 .819l1.563 -.001l-1.614 4.674a1 1 0 0 0 .945 1.327h7.961a1 1 0 0 0 1 -.978l.112 -5c0 -3.827 -1.555 -6.878 -4.67 -7.966l-2.399 -.83l-.375 -.121l-.258 -.074l-.135 -.031l-.101 -.013l-.055 -.001l-.048 .003z"/><path d="M18 18h-12a1 1 0 0 0 -1 1a2 2 0 0 0 2 2h10a2 2 0 0 0 1.987 -1.768l.011 -.174a1 1 0 0 0 -.998 -1.058z"/></svg>';
const ICON_SEARCH = '<svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>';
function renderHeader(route) {
  const items = [
    ['Chơi', '#/', /^$/, [['Trang chủ', '#/'], ['Luật OTT v1', '#/rules']]],
    ['Giải đấu', '#/tournaments', /^(tournaments|tournament)/, [['Tất cả giải', '#/tournaments']]],
    ['Xem', '#/watch', /^(watch|game)/, [['Ván đang diễn ra', '#/watch']]],
  ];
  if (isContestant()) items.push(['Bot của tôi', '#/bot', /^(bot|code)/, [['Các suất của tôi', '#/bot']]]);
  items.push(['Cộng đồng', S.me ? `#/user/${S.me.username}` : '#/tournaments', /^user/, [['Hồ sơ', S.me ? `#/user/${S.me.username}` : '#/login']]]);
  if (isOrg()) items.push(['Quản trị', '#/organizer', /^organizer/, [['Giải của tôi', '#/organizer'], ['Tạo giải mới', '#/organizer/new']]]);
  const nav = items.map(([label, href, re, dd]) => `<div class="nav-item${re.test(route) ? ' active' : ''}"><a href="${href}">${label}</a><div class="dd">${dd.map(([l, h]) => `<a href="${h}">${l}</a>`).join('')}</div></div>`).join('');
  const user = S.me
    ? `<div class="nav-item"><a class="user-link" href="#/user/${esc(S.me.username)}"><span class="online"></span>${esc(S.me.username)}${isOrg() ? ' <span class="tag warn">ORG</span>' : ''}</a>
        <div class="dd"><a href="#/user/${esc(S.me.username)}">Hồ sơ</a>${isContestant() ? '<a href="#/bot">Bot của tôi</a>' : '<a href="#/organizer">Quản trị</a>'}<a href="#/login" data-act="logout">Đăng xuất</a></div></div>`
    : '<a class="button small" href="#/login">Đăng nhập</a>&nbsp;<a class="button green small" href="#/register">Đăng ký</a>';
  $('#top').innerHTML = `<a class="site-title" href="#/" aria-label="OTT Arena">${ICON_KNIGHT}OTTArena<em>.org</em></a><nav class="site-nav" aria-label="Điều hướng chính">${nav}</nav>
    <div class="top-right"><button class="top-icon" aria-label="Tìm kiếm">${ICON_SEARCH}</button>${user}</div>`;
}
const loadingHTML = '<div class="page"><div class="box box-pad dim">Đang tải…</div></div>';
const errorHTML = (msg) => `<div class="page"><div class="box box-pad stack"><h2>Có lỗi</h2><p>${esc(msg)}</p><a class="button" href="#/">Về trang chủ</a></div></div>`;
const needLogin = () => '<div class="page"><div class="box box-pad stack"><h2>Cần đăng nhập</h2><p>Đăng nhập để xem trang này.</p><a class="button green" href="#/login">Đăng nhập</a></div></div>';
function featuredTournament(ts) {
  return ts.find((t) => t.status === 'RUNNING') || ts.find((t) => t.status === 'CODING') || ts.find((t) => t.status === 'REGISTRATION_OPEN') || ts.find((t) => t.status === 'LOCKED') || ts[0] || null;
}
function tourRow(t) {
  const ico = t.status === 'RUNNING' ? '🏆' : t.status === 'CODING' ? '⌨' : t.clock_seconds <= 300 ? '⚡' : '⏱';
  return `<a class="tour-row" href="#/tournament/${t.id}"><span class="ico">${ico}</span><span><div class="tn">${esc(t.name)}</div><div class="ts">${clockLabel(t.clock_seconds)} • OTT v1 • vòng tròn hai lượt</div></span><span class="tp">${tag(t.status)}<br>${t.entries_count} thí sinh</span></a>`;
}
function miniGameHTML(m) {
  const pos = fromServerBoard(m.board);
  const placing = !m.revealed;
  const note = placing ? `<div class="board-hidden-note">Đang xếp quân (bí mật) · ${m.placed.white}/10 · ${m.placed.black}/10</div>` : '';
  const row = (side, name) => `<div class="mg-player"><span class="nm"><span class="online"></span> ${esc(name)}</span>${side === 'black' && placing ? '<span class="tag warn">Xếp quân</span>' : ''}<span class="mg-clock${m.running_side === side ? ' running' : ''}" data-mclock="${m.id}:${side}">${fmtClock(m.clocks_ms[side])}</span></div>`;
  return `<a class="mini-game" href="#/game/${m.id}">${row('black', m.black.username)}<div style="position:relative">${boardHTML(pos, { coords: false, last: m.last_move })}${note}</div>${row('white', m.white.username)}
    <div class="mg-foot"><span>${esc(m.tournament_name)} · V${m.round_no}</span><span>${m.move_count} nước</span></div></a>`;
}
function standingsMini(rows, n = 6) {
  if (!rows.length) return '<div class="box-pad dim">Chưa có thí sinh.</div>';
  return rows.slice(0, n).map((r) => `<div class="lb-row"><span class="rk">${r.rank}</span><span class="title-tag">BOT</span><a href="#/user/${esc(r.username)}" class="user-name">${esc(r.username)}</a><span class="pt">${r.points}</span></div>`).join('');
}
function sheetHTML(sheet) {
  return `<span class="sheet">${sheet.map((c) => `<b class="${c}">${{ w: 3, d: 1, l: 0 }[c]}</b>`).join('') || '<span class="dim">—</span>'}</span>`;
}
const MINI_CLOCKS = { byId: {}, fetchedAt: 0 };
const LIVE_WIDGETS = { ids: '', moves: '', fetching: false, pending: false };
const liveIds = (items) => items.map((m) => m.id).join('|');
const liveMoves = (items) => items.map((m) => `${m.id}:${m.move_count}`).join('|');
function paintMiniClocks() {
  $$('[data-mclock]').forEach((n) => {
    const [id, side] = n.dataset.mclock.split(':'); const m = MINI_CLOCKS.byId[id]; if (!m) return;
    const base = m.clocks_ms[side];
    n.textContent = fmtClock(m.running_side === side ? base - (Date.now() - MINI_CLOCKS.fetchedAt) : base);
  });
}
function setMiniClockData(items, fetchedAt) {
  MINI_CLOCKS.byId = Object.fromEntries(items.map((m) => [m.id, m]));
  MINI_CLOCKS.fetchedAt = fetchedAt;
  paintMiniClocks();
}
function tickMiniClocks(items, fetchedAt) {
  setMiniClockData(items, fetchedAt);
  every(500, paintMiniClocks);
}
function renderLiveWidgets(items) {
  LIVE_WIDGETS.ids = liveIds(items);
  LIVE_WIDGETS.moves = liveMoves(items);
  const home = $('#home-live-cards');
  if (home) home.innerHTML = items.length
    ? `<div class="mini-grid">${items.slice(0, 6).map(miniGameHTML).join('')}</div>`
    : '<div class="box box-pad dim">Chưa có ván nào đang diễn ra.</div>';
  const tv = $('#home-tv-game');
  if (tv && items.length) tv.innerHTML = miniGameHTML(items[0]);
  const watch = $('#watch-live-cards');
  if (watch) watch.innerHTML = items.length
    ? `<div class="mini-grid c4">${items.map(miniGameHTML).join('')}</div>`
    : '<div class="box box-pad dim">Chưa có ván nào đang diễn ra. Ván xuất hiện ở đây khi organizer bắt đầu giải.</div>';
  const count = $('#watch-live-count');
  if (count) { count.textContent = `${items.length} ván`; count.classList.toggle('live', items.length > 0); }
  setMiniClockData(items, Date.now());
}
async function refreshLiveWidgets() {
  if (LIVE_WIDGETS.fetching) { LIVE_WIDGETS.pending = true; return; }
  LIVE_WIDGETS.fetching = true;
  try {
    do {
      LIVE_WIDGETS.pending = false;
      const renderId = routing;
      const live = await api('GET', '/live');
      if (renderId !== routing) return;
      if (liveIds(live.items) !== LIVE_WIDGETS.ids) { route(true); return; }
      renderLiveWidgets(live.items);
    } while (LIVE_WIDGETS.pending);
  } catch { /* next event or fallback poll refreshes */ }
  finally { LIVE_WIDGETS.fetching = false; }
}

/* ================= Trang chủ ================= */
async function pageHome() {
  const renderId = routing;
  const [ts, live, stats] = await Promise.all([api('GET', '/tournaments'), api('GET', '/live'), api('GET', '/stats')]);
  if (renderId !== routing) return '';
  const feat = featuredTournament(ts.items);
  const st = feat ? await api('GET', `/tournaments/${feat.id}/standings?compact=top6`) : { items: [] };
  if (renderId !== routing) return '';
  let myEntry = null;
  if (feat && isContestant()) { try { myEntry = await api('GET', `/tournaments/${feat.id}/entry`); } catch { /* chưa đăng ký */ } }
  if (renderId !== routing) return '';
  const liveItems = live.items;
  const fetchedAt = Date.now();
  let banner = '<div class="live-banner box"><span class="trophy">🏆</span><div><h2>Chưa có giải nào</h2><p>Organizer chưa tạo giải đấu.</p></div></div>';
  if (feat) {
    const cta = feat.status === 'CODING' && myEntry ? `<span class="button green">⌨ Vào viết bot</span>`
      : feat.status === 'RUNNING' ? '<span class="button green">Vào giải</span>'
      : feat.status === 'REGISTRATION_OPEN' && isContestant() && !myEntry ? '<span class="button green">Đăng ký</span>' : '<span class="button">Xem giải</span>';
    const sub = { RUNNING: `Vòng tròn hai lượt · ${feat.entries_count} thí sinh · ${feat.matches_finished}/${feat.matches_total} ván đã xong`,
      CODING: `Giai đoạn viết bot · hạn nộp ${fmtTime(feat.coding_deadline)} · ${feat.entries_count} thí sinh`,
      REGISTRATION_OPEN: `Đang mở đăng ký · ${feat.entries_count} thí sinh`, LOCKED: 'Đã khóa danh sách, chờ bắt đầu', FINISHED: 'Giải đã kết thúc', DRAFT: 'Nháp', CANCELLED: 'Đã hủy' }[feat.status];
    const href = feat.status === 'CODING' && myEntry ? `#/code/${feat.id}` : `#/tournament/${feat.id}`;
    banner = `<a class="live-banner box" href="${href}" style="color:inherit;text-decoration:none"><span class="trophy">🏆</span>
      <div><h2>${feat.status === 'RUNNING' ? '<span class="pulse"></span>' : ''}${esc(feat.name)}</h2><p>${esc(sub)}</p></div><span class="spacer"></span>${cta}</a>`;
  }
  const tv = liveItems[0];
  LIVE_WIDGETS.ids = liveIds(liveItems);
  LIVE_WIDGETS.moves = liveMoves(liveItems);
  tickMiniClocks(liveItems, fetchedAt);
  poll(3000, async () => {
    if (renderId !== routing || LIVE.ok) return;
    try {
      const l = await api('GET', '/live');
      if (renderId !== routing) return;
      if (liveIds(l.items) !== LIVE_WIDGETS.ids) route(true);
      else if (liveMoves(l.items) !== LIVE_WIDGETS.moves) renderLiveWidgets(l.items);
    } catch { /* bỏ qua */ }
  });
  return `<div class="page"><div class="lobby">
    <div class="lobby__col left">
      <section class="box">${S.me ? `<div class="box__title">Xin chào, ${esc(S.me.username)}</div><div class="lobby-actions" style="padding-top:1rem">
          ${isContestant() ? '<a class="button big" href="#/bot">Bot của tôi</a>' : '<a class="button big" href="#/organizer">Quản trị giải</a>'}<a class="button big" href="#/watch">Xem ván đang chơi</a><a class="button big" href="#/rules">Luật OTT v1</a></div>`
        : `<div class="box__title">Chào mừng</div><div class="lobby-actions" style="padding-top:1rem"><a class="button green big" href="#/login">Đăng nhập</a><a class="button big" href="#/register">Tạo tài khoản</a><a class="button big" href="#/rules">Luật OTT v1</a></div>`}</section>
      <section class="box"><div class="box__title">Giải đấu <a href="#/tournaments">Tất cả »</a></div>${ts.items.slice(0, 5).map(tourRow).join('') || '<div class="box-pad dim">Chưa có giải.</div>'}</section>
    </div>
    <div class="lobby__col mid">${banner}
      <section aria-label="Các ván đang diễn ra"><div class="row" style="margin-bottom:.7rem"><h2 style="font-size:1.25rem">Ván đang diễn ra</h2><span class="spacer"></span><a href="#/watch">Xem tất cả »</a></div>
        <div id="home-live-cards">${liveItems.length ? `<div class="mini-grid">${liveItems.slice(0, 6).map(miniGameHTML).join('')}</div>` : '<div class="box box-pad dim">Chưa có ván nào đang diễn ra.</div>'}</div></section>
    </div>
    <div class="lobby__col right">
      <section class="box"><div class="counters"><span><b>${stats.users}</b> tài khoản</span><span><b>${stats.running_matches}</b> ván đang chơi</span></div></section>
      ${tv ? `<section class="box tv-box"><div class="box__title" style="padding:.3rem .3rem .6rem;border:0">OTT TV <a href="#/game/${tv.id}">Xem »</a></div><div id="home-tv-game">${miniGameHTML(tv)}</div></section>` : ''}
      <section class="box"><div class="box__title">Bảng xếp hạng ${feat ? `<a href="#/tournament/${feat.id}">Đầy đủ »</a>` : ''}</div>${standingsMini(st.items)}</section>
    </div></div></div>`;
}

/* ================= Danh sách giải & chi tiết giải ================= */
async function pageTournaments() {
  const ts = (await api('GET', '/tournaments')).items;
  return `<div class="page"><div class="lobby" style="grid-template-columns:minmax(0,1fr) minmax(250px,320px)"><div class="lobby__col">
    <section class="box"><div class="box__top"><h1>Giải đấu</h1>${isOrg() ? '<a class="button green" href="#/organizer/new">＋ Tạo giải</a>' : ''}</div>
    <table class="slist"><thead><tr><th>Giải</th><th>Đồng hồ</th><th>Thể thức</th><th>Trạng thái</th><th class="r">Thí sinh</th></tr></thead><tbody>
    ${ts.map((t) => `<tr><td><a href="#/tournament/${t.id}" class="user-name">${t.status === 'RUNNING' ? '🏆' : '♞'} ${esc(t.name)}</a><div class="dim" style="font-size:.85rem">${t.matches_total ? `${t.matches_finished}/${t.matches_total} ván` : `Viết bot ${t.coding_seconds / 60} phút`}</div></td><td>${clockLabel(t.clock_seconds)}</td><td>Vòng tròn hai lượt</td><td>${tag(t.status)}</td><td class="r">${t.entries_count}</td></tr>`).join('') || '<tr><td colspan="5" class="dim">Chưa có giải nào.</td></tr>'}
    </tbody></table></section></div>
    <div class="lobby__col"><section class="box"><div class="box__title">Thể thức OTT v1</div><div class="stat-list"><div><span>Bàn cờ</span><b>9×9</b></div><div><span>Mỗi bên</span><b>3 Kéo · 3 Búa · 3 Bao · 1 Vua</b></div><div><span>Viết bot</span><b>trên web, nộp 1 lần</b></div><div><span>Thắng · Hòa · Thua</span><b>3 · 1 · 0</b></div><div><span>Ngôn ngữ</span><b>C++17 · C17 · Rust 2021 · Go</b></div></div></section></div></div></div>`;
}

async function pageTournament(id, q) {
  const renderId = routing;
  const tab = q.get('tab') || 'standings';
  const offset = Math.max(0, Number.parseInt(q.get('offset') || '0', 10) || 0);
  const scheduleParams = new URLSearchParams({ pagination: 'keyset', limit: '100', offset: String(offset) });
  const scheduleCursor = q.get('cursor');
  if (scheduleCursor) {
    scheduleParams.set('cursor', scheduleCursor);
    scheduleParams.set('direction', q.get('direction') || 'after');
  }
  const [t, standingsData] = await Promise.all([
    api('GET', `/tournaments/${id}`),
    tab === 'standings' ? api('GET', `/tournaments/${id}/standings`) : Promise.resolve({ items: [] }),
  ]);
  let st = standingsData;
  if (renderId !== routing) return '';
  syncOffset(t.server_time);
  if (tab === 'pairings' && t.status === 'FINISHED') {
    st = await api('GET', `/tournaments/${id}/standings?compact=top3`);
    if (renderId !== routing) return '';
  }
  const sched = tab === 'pairings'
    ? await api('GET', `/tournaments/${id}/schedule?${scheduleParams}`)
    : { matches: [], total: 0, has_more: false, limit: 100, offset };
  if (renderId !== routing) return '';
  let entry = null;
  if (isContestant()) { try { entry = await api('GET', `/tournaments/${id}/entry`); } catch { /* chưa đăng ký */ } }
  if (renderId !== routing) return '';
  const liveDetail = t.matches_running
    ? (await api('GET', `/live?tournament_id=${encodeURIComponent(id)}`)).items
    : [];
  if (renderId !== routing) return '';
  const mine = S.me ? liveDetail.find((m) => m.white.username === S.me.username || m.black.username === S.me.username) : null;
  let actions = '';
  if (t.status === 'REGISTRATION_OPEN' && isContestant()) actions = entry ? '<span class="tag ok">Đã đăng ký</span><span class="dim">Chờ organizer bắt đầu giai đoạn viết bot</span>' : `<button class="button green" data-act="register" data-tid="${id}">Đăng ký tham gia</button>`;
  if (t.status === 'REGISTRATION_OPEN' && !S.me) actions = '<a class="button green" href="#/login">Đăng nhập để đăng ký</a>';
  if (t.status === 'CODING' && entry) actions = `<span class="tag warn">Đang viết bot</span><span class="spacer"></span><a class="button green" href="#/code/${id}">⌨ ${entry.submitted_at ? 'Xem bài đã nộp' : 'Viết & nộp bot'}</a>`;
  if (t.status === 'RUNNING' && mine) actions = `<span class="spacer"></span><a class="button green" href="#/game/${mine.id}">▶ Ván của tôi</a>`;
  const countdown = t.status === 'CODING' ? `<div class="tour__clock" id="tour-clock">${fmtCountdown(t.coding_deadline - nowSrv())}<small>còn lại để nộp bot</small></div>` : '';
  if (t.status === 'CODING') every(500, () => { const el = $('#tour-clock'); if (el) el.firstChild.textContent = fmtCountdown(t.coding_deadline - nowSrv()); });
  if (t.status === 'RUNNING') every(10000, async () => {
    if (renderId !== routing || !location.hash.startsWith(`#/tournament/${id}`) || LIVE.ok) return;
    try {
      const latest = await api('GET', `/tournaments/${id}`);
      if (renderId === routing && (latest.status !== t.status || latest.matches_finished !== t.matches_finished || latest.matches_running !== t.matches_running)) route(true);
    } catch { /* SSE reconnect or next fallback poll will refresh */ }
  });
  const rounds = {};
  sched.matches.forEach((m) => { (rounds[`Lượt ${m.leg_no} · Vòng ${m.round_no}`] ||= []).push(m); });
  const pair = Object.entries(rounds).map(([r, ms]) => `<div class="pair-round">${r}</div>${ms.map((m) => `<div class="pair-row"><span class="dim">#${m.pairing_no}</span><span><span class="dot w" style="display:inline-block;width:.8rem;height:.8rem;border-radius:50%;background:#eee;margin-right:.4rem"></span><a class="user-name" href="#/user/${esc(m.white.username)}">${esc(m.white.username)}</a></span>
      <span class="sc">${m.status === 'FINISHED' ? SCORE[m.result] : m.status === 'RUNNING' ? '<span class="tag live">LIVE</span>' : m.status === 'PAUSED' ? '<span class="tag err">Tạm dừng</span>' : '<span class="tag">Chờ</span>'}</span>
      <span class="r"><a class="user-name" href="#/user/${esc(m.black.username)}">${esc(m.black.username)}</a> <span class="dot b" style="display:inline-block;width:.8rem;height:.8rem;border-radius:50%;background:#222;border:1px solid #777;margin-left:.4rem"></span></span><span class="r"><a href="#/game/${m.id}">${m.status === 'SCHEDULED' ? '' : 'Xem'}</a></span></div>`).join('')}`).join('');
  const pairPage = sched.total ? `<div class="row" style="padding:1rem;justify-content:center;gap:.75rem"><a class="button small ${sched.previous_cursor ? '' : 'disabled'}" href="#/tournament/${id}?tab=pairings&offset=${Math.max(0, offset - sched.limit)}&cursor=${encodeURIComponent(sched.previous_cursor || '')}&direction=before">Trước</a><span class="dim">${offset + 1}–${Math.min(offset + sched.matches.length, sched.total)} / ${sched.total}</span><a class="button small ${sched.next_cursor ? '' : 'disabled'}" href="#/tournament/${id}?tab=pairings&offset=${offset + sched.limit}&cursor=${encodeURIComponent(sched.next_cursor || '')}&direction=after">Tiếp</a></div>` : '';
  const podium = t.status === 'FINISHED' && st.items.length >= 3 ? `<section class="box"><div class="box__title">Top 3</div><div class="podium"><div><span class="medal">🥈</span><span>${esc(st.items[1].username)}</span><small>${st.items[1].points}</small></div><div class="p1"><span class="medal">🥇</span><span>${esc(st.items[0].username)}</span><small>${st.items[0].points}</small></div><div><span class="medal">🥉</span><span>${esc(st.items[2].username)}</span><small>${st.items[2].points}</small></div></div></section>` : '';
  const fetchedAt = Date.now();
  tickMiniClocks(liveDetail, fetchedAt);
  return `<div class="page"><div class="tour">
    <aside class="tour__side stack"><section class="box game-info"><div class="tc"><span class="ico">⏱</span><div><b>${clockLabel(t.clock_seconds)} • OTT v1</b><small>Vòng tròn hai lượt</small></div></div><hr>
      <div class="stat-list" style="padding:0"><div><span>Thí sinh</span><b>${t.entries_count}</b></div><div><span>Ván đã xong</span><b>${t.matches_finished}/${t.matches_total}</b></div><div><span>Thời gian viết bot</span><b>${t.coding_seconds / 60} phút</b></div><div><span>Ngôn ngữ</span><b>C++17 · C17 · Rust 2021 · Go</b></div></div></section>
      ${t.status === 'FINISHED' || t.status === 'RUNNING' ? '' : '<div class="notice">Bot được viết ngay trên web trong giai đoạn viết bot, nộp một lần và không thể sửa sau khi nộp.</div>'}</aside>
    <div class="stack" style="min-width:0"><section class="box"><div class="tour__head"><span class="trophy">🏆</span><div><h1>${esc(t.name)}</h1><div class="meta">${tag(t.status)} &nbsp;${clockLabel(t.clock_seconds)} • vòng tròn hai lượt</div></div>${countdown}</div>
      ${actions ? `<div class="row" style="padding:0 1.3rem 1rem">${actions}</div>` : ''}
      <div class="tabs"><a class="${tab === 'standings' ? 'on' : ''}" href="#/tournament/${id}">Bảng điểm</a><a class="${tab === 'pairings' ? 'on' : ''}" href="#/tournament/${id}?tab=pairings">Lịch đấu</a></div>
      ${tab === 'standings' ? `<table class="slist"><thead><tr><th>#</th><th>Thí sinh</th><th>Kết quả các ván</th><th class="r">Điểm</th></tr></thead><tbody>${st.items.map((r) => `<tr class="${S.me && r.username === S.me.username ? 'me' : ''}"><td class="rankn">${r.rank}</td><td><span class="title-tag">BOT</span><a href="#/user/${esc(r.username)}" class="user-name">${esc(r.username)}</a></td><td>${sheetHTML(r.sheet)}</td><td class="r"><b class="user-name">${r.points}</b></td></tr>`).join('') || '<tr><td colspan="4" class="dim">Chưa có thí sinh đăng ký.</td></tr>'}</tbody></table>`
        : (pair ? pair + pairPage : '<div class="box-pad dim">Chưa sinh lịch đấu. Organizer sinh lịch sau khi khóa roster.</div>')}</section></div>
    <aside class="stack">${liveDetail.length ? `<section class="box tv-box"><div class="box__title" style="padding:.3rem .3rem .6rem;border:0">Ván đang diễn ra <a href="#/watch">Tất cả »</a></div>${miniGameHTML(liveDetail[0])}</section>` : ''}${podium}
      <section class="box"><div class="box__title">Thống kê giải</div><div class="stat-list"><div><span>Ván đang chạy</span><b>${t.matches_running}</b></div><div><span>Bắt đầu</span><b>${fmtDate(t.started_at)}</b></div><div><span>Kết thúc</span><b>${fmtDate(t.finished_at)}</b></div></div></section></aside>
  </div></div>`;
}

/* ================= Trang ván đấu ================= */
const G = { m: null, positions: [], ply: 0, flip: false, fetchedAt: 0, refresh: null };
function paintGame() {
  const root = $('.round'); if (!root || !G.m) return;
  const m = G.m, last = G.positions.length - 1;
  const pos = G.positions[G.ply] || {};
  const lm = G.ply > 0 ? m.moves[G.ply - 1] : null;
  $('#board-wrap').innerHTML = boardHTML(pos, { flip: G.flip, last: lm ? { from: lm.from, to: lm.to } : null });
  const rows = [];
  for (let i = 0; i < m.moves.length; i += 2) {
    const cell = (mv, idx) => mv ? `<button class="mv${mv.captured ? ' cap' : ''}${G.ply === idx + 1 ? ' on' : ''}" data-ply="${idx + 1}">${san(mv)}</button>` : '<span></span>';
    rows.push(`<div class="mrow"><span class="mnum">${i / 2 + 1}</span>${cell(m.moves[i], i)}${cell(m.moves[i + 1], i + 1)}</div>`);
  }
  let status = '';
  if (m.status === 'FINISHED') {
    const who = m.result === 'DRAW' ? 'Hòa' : `${m.result === 'WHITE_WIN' ? 'Trắng' : 'Đen'} thắng`;
    status = `<div class="result-box"><strong>${SCORE[m.result]}</strong>${who} · ${REASONS[m.result_reason] || m.result_reason}</div>`;
  } else if (m.status === 'PAUSED') status = '<div class="status">Trận tạm dừng (lỗi hạ tầng) — chờ organizer khôi phục</div>';
  else if (m.status === 'SCHEDULED') status = '<div class="status">Đang chờ tới lượt chạy…</div>';
  else if (!m.revealed) status = '<div class="status">Đang xếp quân (bí mật)<span class="thinking"><i></i><i></i><i></i></span></div>';
  else if (G.ply === last) status = `<div class="status">${esc((m.running_side || (m.side_to_act || '').toLowerCase()) === 'white' ? m.white.username : m.black.username)} ${m.running_side ? 'đang suy nghĩ' : 'chuẩn bị đi'}<span class="thinking"><i></i><i></i><i></i></span></div>`;
  const mv = $('#moves'); const keep = mv.scrollTop;
  mv.innerHTML = rows.join('') + status;
  const on = G.ply === last ? null : mv.querySelector('.mv.on');
  if (on) on.scrollIntoView({ block: 'nearest' }); else if (G.ply === last) mv.scrollTop = mv.scrollHeight; else mv.scrollTop = keep;
  const top = G.flip ? 'white' : 'black', bot = G.flip ? 'black' : 'white';
  const live = m.status === 'RUNNING' && G.ply === last;
  const strip = (side) => `<span class="online"></span><div style="flex:1"><div class="nm"><span class="title-tag">BOT</span><a href="#/user/${esc(m[side].username)}">${esc(m[side].username)}</a>${m.revealed ? capturedBy(G.positions, G.ply, side === 'white' ? 'b' : 'w') : ''}</div><div class="sub">${m.revealed ? '' : `đã đặt ${m.placed[side]}/10 quân`}</div></div>
      <div class="rclock${live && m.running_side === side ? ' running' : ''}" data-clock="${side}">${fmtClock(m.clocks_ms[side])}</div>`;
  $('#pl-top').innerHTML = strip(top); $('#pl-bot').innerHTML = strip(bot);
  $$('[data-nav="flip"]').forEach((b) => b.classList.toggle('on', G.flip));
  $$('[data-nav="first"],[data-nav="prev"]').forEach((b) => { b.disabled = G.ply === 0; });
  $$('[data-nav="next"],[data-nav="last"]').forEach((b) => { b.disabled = G.ply === last; });
  const note = $('#placed-note'); if (note) note.hidden = m.revealed;
  const cnt = $('#mv-count'); if (cnt) cnt.textContent = m.move_count;
  const stt = $('#m-status'); if (stt) stt.textContent = m.status;
  const badge = $('#game-status');
  if (badge) {
    badge.textContent = m.status === 'RUNNING' ? 'ĐANG ĐẤU' : m.status === 'FINISHED' ? 'KẾT THÚC' : m.status;
    badge.classList.toggle('live', m.status === 'RUNNING');
    badge.classList.toggle('warn', m.status !== 'RUNNING' && m.status !== 'FINISHED');
  }
}
function setMatch(m, follow) {
  const wasLast = G.ply === G.positions.length - 1;
  const { positions, changed: historyChanged } = matchPositions(m);
  G.m = m; G.positions = positions; G.fetchedAt = Date.now();
  if (follow || wasLast) G.ply = G.positions.length - 1;
  G.ply = Math.min(G.ply, G.positions.length - 1);
  return historyChanged;
}
function tickClocks() {
  const m = G.m; if (!m || m.status !== 'RUNNING' || !m.running_side) return;
  const el = $(`[data-clock="${m.running_side}"]`); if (!el) return;
  const ms = m.clocks_ms[m.running_side] - (Date.now() - G.fetchedAt);
  el.textContent = fmtClock(ms); el.classList.toggle('low', ms < 30000);
}
async function pageGame(id) {
  const renderId = routing;
  const m = await api('GET', `/matches/${id}`);
  if (renderId !== routing) return '';
  G.ply = 0; G.flip = !!(S.me && m.black.username === S.me.username);
  setMatch(m, true);
  const own = m.viewer_is_player;
  let poll = null, clock = null, pending = null, again = false, tick = 0;
  let finished = m.status === 'FINISHED';
  const active = () => renderId === routing && G.m?.id === id;
  const eligible = () => active() && !finished && !document.hidden && Date.now() >= NET.state.until;
  const refresh = (followUp = true) => {
    if (!active() || finished) return Promise.resolve(false);
    if (!eligible() || pending) {
      if (followUp) again = true;
      return pending || Promise.resolve(false);
    }
    pending = (async () => {
      do {
        again = false;
        try {
          const since = Number.isInteger(G.m.state_version) ? `?since=${G.m.state_version}` : '';
          const nm = await api('GET', `/matches/${id}${since}`);
          if (!active()) return false;
          let changed, historyChanged = false;
          if (nm.not_modified) {
            changed = nm.status !== G.m.status || nm.result !== G.m.result || nm.result_reason !== G.m.result_reason;
            G.m = { ...G.m, status: nm.status, result: nm.result, result_reason: nm.result_reason,
              clocks_ms: nm.clocks_ms, running_side: nm.running_side };
            G.fetchedAt = Date.now();
          } else {
            changed = nm.move_count !== G.m.move_count || nm.status !== G.m.status || nm.revealed !== G.m.revealed || nm.placed.white !== G.m.placed.white || nm.placed.black !== G.m.placed.black;
            historyChanged = setMatch(nm, false);
          }
          finished = nm.status === 'FINISHED';
          if (finished) { NET.stopPoll(poll); clearInterval(clock); G.refresh = null; }
          if (changed || historyChanged) paintGame();
        } catch { return false; }
      } while (again && eligible());
      return true;
    })().finally(() => { pending = null; });
    return pending;
  };
  // push (SSE) cập nhật ngay; polling chỉ là lưới an toàn (5 giây khi stream đang nối, 1 giây khi mất stream; chế độ tunnel: 10 giây)
  if (!finished) {
    G.refresh = refresh;
    poll = NET.poll(1000, async () => { if (LIVE.ok && !again && ++tick % 5) return; await refresh(false); });
    clock = every(250, tickClocks);
  }
  const wName = esc(m.white.username), bName = esc(m.black.username);
  return `<div class="page"><div class="round">
    <div class="round__side">
      <section class="box game-info"><div class="tc"><span class="ico">⏱</span><div><b>${clockLabel(m.clock_seconds)} • OTT v1</b><small>${esc(m.tournament_name)} · Lượt ${m.leg_no} · Vòng ${m.round_no}</small></div></div><hr>
        <div class="pl-line"><span class="dot w"></span><a class="user-name" href="#/user/${wName}">${wName}</a>${own && S.me?.username === m.white.username ? '<span class="tag info" style="margin-left:auto">BẠN</span>' : ''}</div>
        <div class="pl-line"><span class="dot b"></span><a class="user-name" href="#/user/${bName}">${bName}</a>${own && S.me?.username === m.black.username ? '<span class="tag info" style="margin-left:auto">BẠN</span>' : ''}</div></section>
      ${own ? '<div class="notice ok">Bot của bạn tự đấu — bạn chỉ theo dõi. Bạn không thể đi quân, xin hòa hay đầu hàng.</div>' : '<div class="notice">Bạn đang xem ván của thí sinh khác. Bố trí quân chỉ được lộ sau khi hai bên xếp xong.</div>'}
      <div class="notice warn" id="placed-note" ${m.revealed ? 'hidden' : ''}>Giai đoạn xếp quân: vị trí quân của cả hai bên được giấu cho tới khi bắt đầu đi quân.</div>
      <a class="button outline" href="#/tournament/${m.tournament_id}">← Về ${esc(m.tournament_name)}</a>
    </div>
    <div class="round__board"><div id="board-wrap"></div>
      <div class="under"><div class="tabs"><button class="on" data-tab-group="und" data-tab="info">Thông tin</button><button data-tab-group="und" data-tab="rules">Quân &amp; luật</button></div>
        <div class="pane tab-pane on" data-pane="und:info"><div class="stat-list" style="padding:0"><div><span>Trạng thái</span><b id="m-status">${m.status}</b></div><div><span>Số nước đi</span><b id="mv-count">${m.move_count}</b></div><div><span>Đồng hồ</span><b>${clockLabel(m.clock_seconds)} mỗi bên</b></div></div></div>
        <div class="pane tab-pane" data-pane="und:rules"><div class="row" style="gap:1.4rem;flex-wrap:wrap">${['k', 'b', 'a', 'v'].map((t) => `<span class="row"><svg viewBox="0 0 100 100" width="34" height="34"><use href="#pc-w${t}"/></svg><span><b class="user-name">${PNAME[t]}</b><div class="dim" style="font-size:.85rem">${{ k: 'bắt Bao', b: 'bắt Kéo', a: 'bắt Búa', v: 'đi 1 ô, không bắt' }[t]}</div></span></span>`).join('')}</div></div></div></div>
    <div class="round__app"><div class="row" style="justify-content:space-between">${m.status === 'RUNNING' ? '<span id="game-status" class="tag live">ĐANG ĐẤU</span>' : m.status === 'FINISHED' ? '<span id="game-status" class="tag">KẾT THÚC</span>' : `<span id="game-status" class="tag warn">${m.status}</span>`}<span class="dim">${own ? 'Ván của bạn' : 'Đang xem'}</span></div>
      <div class="rplayer" id="pl-top"></div><div class="moves" id="moves"></div>
      <div class="rcontrols"><button data-nav="first" aria-label="Về đầu">⏮</button><button data-nav="prev" aria-label="Nước trước">◀</button><button data-nav="next" aria-label="Nước sau">▶</button><button data-nav="last" aria-label="Về cuối">⏭</button><span class="sep"></span><button data-nav="flip" aria-label="Xoay bàn" title="Xoay bàn (f)">⇅</button></div>
      <div class="rplayer" id="pl-bot"></div></div></div></div>`;
}

/* ================= Xem nhiều ván ================= */
async function pageWatch() {
  const renderId = routing;
  const [live, ts] = await Promise.all([api('GET', '/live'), api('GET', '/tournaments')]);
  if (renderId !== routing) return '';
  const feat = featuredTournament(ts.items);
  const recent = feat ? (await api('GET', `/tournaments/${feat.id}/schedule?recent=12`)).matches.reverse() : [];
  if (renderId !== routing) return '';
  LIVE_WIDGETS.ids = liveIds(live.items);
  LIVE_WIDGETS.moves = liveMoves(live.items);
  tickMiniClocks(live.items, Date.now());
  poll(4000, async () => {
    if (renderId !== routing || LIVE.ok) return;
    try {
      const l = await api('GET', '/live');
      if (renderId !== routing) return;
      if (liveIds(l.items) !== LIVE_WIDGETS.ids) route(true);
      else if (liveMoves(l.items) !== LIVE_WIDGETS.moves) renderLiveWidgets(l.items);
    } catch { /* bỏ qua */ }
  });
  return `<div class="page"><div class="stack">
    <section class="box"><div class="box__top"><h1>Ván đang diễn ra</h1><span id="watch-live-count" class="tag ${live.items.length ? 'live' : ''}">${live.items.length} ván</span></div></section>
    <div id="watch-live-cards">${live.items.length ? `<div class="mini-grid c4">${live.items.map(miniGameHTML).join('')}</div>` : '<div class="box box-pad dim">Chưa có ván nào đang diễn ra. Ván xuất hiện ở đây khi organizer bắt đầu giải.</div>'}</div>
    ${recent.length ? `<section class="box"><div class="box__title">Ván vừa kết thúc · ${esc(feat.name)}</div><table class="slist"><tbody>${recent.map((m) => `<tr><td>${esc(m.white.username)}</td><td>${SCORE[m.result]}</td><td>${esc(m.black.username)}</td><td class="dim">${REASONS[m.result_reason] || ''}</td><td class="r"><a href="#/game/${m.id}">Xem lại</a></td></tr>`).join('')}</tbody></table></section>` : ''}
  </div></div>`;
}

/* ================= Viết bot (code editor, đếm ngược) ================= */
let C = { tid: null, owner: null, editable: false, dirty: false, saveTimer: null, savePromise: null };
const REFERENCE = `<details open><summary>Giao thức bot</summary>
<p>Mỗi lượt engine gửi <b>một dòng JSON</b> (observation) qua stdin. Bot trả <b>một dòng JSON</b> (action) qua stdout. Chẩn đoán in ra <code>stderr</code>.</p>
<pre class="logbox">{"phase":"play","side":"white",
 "board":{"e4":{"side":"white","piece":"paper","promotion":null}},
 "legal_actions":[{"type":"move","from":"e4","to":"e5"}],
 "clocks_ms":{"white":600000,"black":600000},...}</pre>
<pre class="logbox">{"type":"place","piece":"king","to":"e1"}
{"type":"move","from":"e4","to":"e5"}
{"type":"move","from":"e8","to":"e9","promotion":"rook"}</pre></details>
<details><summary>Luật tóm tắt</summary><ul><li>Bàn 9×9; mỗi bên 3 Kéo, 3 Búa, 3 Bao, 1 Vua.</li><li>Trắng đặt quân trước ở hàng 1–2, Đen ở hàng 8–9; vị trí được giấu tới khi bắt đầu đi quân.</li><li>Kéo bắt Bao, Bao bắt Búa, Búa bắt Kéo; chỉ bắt khi tiến thẳng hoặc chéo tiến.</li><li>Tới hàng cuối: phong cấp Xe hoặc Tượng.</li><li>Bắt Vua thắng. Hết giờ, action sai, crash: thua.</li><li>Chỉ được trả action có trong <code>legal_actions</code>.</li></ul></details>
<details><summary>Mẹo</summary><ul><li>Chỉ sửa hàm <code>choose()</code> giữa hai dòng YOUR STRATEGY.</li><li>Mỗi lần biên dịch tốn 1 trong __LIMIT__ lượt; nộp đúng bản vừa biên dịch thành công không tốn thêm lượt.</li></ul></details>`;
async function pageCode(tid) {
  if (!S.me) return needLogin();
  if (!isContestant()) return errorHTML('Chỉ thí sinh mới viết bot.');
  const renderId = routing;
  const previous = C.tid === tid && C.owner === S.me.username ? C : null;
  if (previous && (previous.dirty || previous.savePromise)) await saveDraft(previous);
  if (renderId !== routing) return '';
  let d;
  try { d = await api('GET', `/tournaments/${tid}/code`); }
  catch (e) { if (e.code === 'NO_ENTRY') return `<div class="page"><div class="box box-pad stack"><h2>Bạn chưa đăng ký giải này</h2><a class="button green" href="#/tournament/${tid}">Về trang giải</a></div></div>`; throw e; }
  if (renderId !== routing) return '';
  syncOffset(d.server_time);
  const t = d.tournament;
  const source = previous?.dirty && d.editable ? previous.source : d.source;
  const savedSource = d.is_template ? null : d.source;
  const languages = d.languages || [{ id: 'cpp17', label: 'C++17', filename: 'bot.cpp' }];
  const language = previous?.dirty && d.editable ? previous.language || 'cpp17' : d.language || 'cpp17';
  C = { tid, owner: S.me.username, deadline: t.coding_deadline || 0, editable: d.editable,
    source, savedSource, language, savedLanguage: d.language || 'cpp17', languages, savedAt: d.updated_at, dirty: d.editable && (source !== savedSource || language !== (d.language || 'cpp17')) && (!d.is_template || !!previous?.dirty),
    saveTimer: null, savePromise: null, resumeSave: null, revision: 0, templateRequest: null,
    editor: null, used: d.compile_used, limit: d.compile_limit };
  const submitted = d.submitted;
  const waiting = t.status === 'REGISTRATION_OPEN';
  PAGE.waiting = waiting;
  const pending = d.pending_submission;
  const expired = t.status === 'CODING' && !submitted && !pending && !d.editable;
  if (d.editable) every(500, codeTick);
  if (t.status === 'CODING' && !submitted && (!d.submit_note || pending?.status === 'QUEUED')) poll(10000, async () => { if (renderId !== routing) return; if (!C.editable && !document.hidden) { try { const r = await api('GET', `/tournaments/${tid}/code`); if (renderId === routing && (r.submitted || r.submit_note || JSON.stringify(r.pending_submission) !== JSON.stringify(pending))) route(true); } catch { /* bỏ qua */ } } });
  if (waiting) poll(2000, async () => { if (renderId !== routing) return; try { const r = await api('GET', `/tournaments/${tid}/code`); if (renderId === routing && r.tournament.status !== 'REGISTRATION_OPEN') route(true); } catch { /* bỏ qua */ } });
  const banner = submitted ? `<div class="notice ok">🔒 Đã nộp lúc <b>${fmtDate(d.submitted_at)}</b>. Bài nộp là bản cuối cùng và <b>không thể thay đổi</b>.</div>`
    : pending ? `<div class="notice ${pending.status === 'REVIEW' ? 'warn' : ''}">Nguồn đã được nhận lúc <b>${fmtDate(pending.received_at)}</b> và đã khóa. ${pending.status === 'REVIEW' ? 'Cần ban tổ chức kiểm tra sự cố hạ tầng.' : 'Đang chờ biên dịch; thời gian hàng đợi không làm bài nộp trễ hạn.'}</div>`
    : d.submit_note ? `<div class="notice warn">⚠ ${esc(d.submit_note)}</div>`
    : waiting ? '<div class="notice">Giai đoạn viết bot chưa bắt đầu. Bạn có thể xem mã mẫu; khi organizer bấm bắt đầu, đồng hồ đếm ngược sẽ chạy.</div>'
    : expired ? '<div class="notice warn">⏱ Hết giờ — hệ thống đang tự nộp bản nháp đã lưu…</div>' : '';
  return `<div class="page"><div class="code-layout">
    <section class="box code-main"><div class="box__top"><h1 id="code-filename">${esc(languages.find((item) => item.id === language)?.filename || 'bot.cpp')}</h1><label>Ngôn ngữ <select id="code-language" ${d.editable ? '' : 'disabled'}>${languages.map((item) => `<option value="${esc(item.id)}" ${item.id === language ? 'selected' : ''}>${esc(item.label)}</option>`).join('')}</select></label><span class="spacer"></span><span class="dim" id="save-state">${submitted ? 'Chỉ đọc' : d.updated_at ? `Đã lưu lúc ${fmtTime(d.updated_at)}` : 'Mã mẫu — chưa lưu'}</span></div>
      ${banner}
      <div class="editor"><pre class="gutter" id="gutter" aria-hidden="true"></pre><textarea id="code" spellcheck="false" wrap="off" autocapitalize="off" autocomplete="off" aria-label="Mã nguồn bot" ${d.editable ? '' : 'readonly'}>${esc(source)}</textarea></div>
      <div class="code-actions">${d.editable ? '<button class="button" data-act="compile-check" id="btn-check">▶ Biên dịch thử</button><button class="button outline" data-act="reset-template">Khôi phục mã mẫu</button><span class="spacer"></span><button class="button green big" data-act="submit-open" id="btn-submit">Nộp bài (không thể sửa)</button>' : `<a class="button" href="#/tournament/${tid}">← Về trang giải</a>`}</div>
      <div class="box__title" style="border-top:1px solid var(--line)">Kết quả biên dịch</div><div class="logbox" id="compile-log" style="margin:1rem;max-height:13rem">${submitted ? '<span class="g">Bản nộp đã biên dịch thành công.</span>' : 'Chưa biên dịch. Nhấn “Biên dịch thử” để kiểm tra mã.'}</div></section>
    <aside class="stack"><section class="box timer-box ${submitted ? 'done' : ''}" id="timer-box"><div class="timer-label">${submitted ? 'ĐÃ NỘP' : waiting ? 'CHƯA BẮT ĐẦU' : 'THỜI GIAN CÒN LẠI'}</div>
        <div class="timer" id="timer">${submitted ? '🔒' : waiting ? fmtCountdown(t.coding_seconds) : fmtCountdown(t.coding_deadline - nowSrv())}</div>
        <div class="timer-sub">${t.coding_deadline ? `Hạn nộp: ${fmtTime(t.coding_deadline)}` : `${t.coding_seconds / 60} phút kể từ khi bắt đầu`}</div></section>
      <section class="box"><div class="stat-list"><div><span>Giải</span><b>${esc(t.name)}</b></div><div><span>Lượt biên dịch</span><b id="compile-used">${d.compile_used}/${d.compile_limit}</b></div><div><span>Trạng thái</span><b>${submitted ? 'Đã nộp' : d.editable ? 'Đang viết' : waiting ? 'Chờ bắt đầu' : 'Hết giờ'}</b></div></div></section>
      <section class="box box-pad ref">${REFERENCE.replace('__LIMIT__', d.compile_limit)}</section></aside></div>
    <div class="modal" id="submit-modal" hidden><div class="modal-card box"><h2>Nộp bài?</h2><p>Sau khi nộp, mã nguồn là <b>bản cuối cùng</b> và <b>không thể thay đổi</b>. Hệ thống biên dịch lại nếu bản này chưa được biên dịch thử thành công.</p>
      <label class="switch"><input type="checkbox" id="submit-confirm"><span>Tôi hiểu và muốn nộp bản này</span></label><div class="form-actions"><button class="button" data-act="submit-cancel">Quay lại</button><button class="button green" data-act="submit-go" id="submit-go" disabled>Nộp bài</button></div></div></div></div>`;
}
function codeTick() {
  const left = C.deadline - nowSrv();
  const el = $('#timer'); if (!el) return;
  el.textContent = fmtCountdown(left);
  $('#timer-box')?.classList.toggle('low', left < 300);
  if (left <= 0 && C.editable) {
    C.editable = false;
    $('#code')?.setAttribute('readonly', '');
    if ($('#code-language')) $('#code-language').disabled = true;
    $$('.code-actions .button').forEach((b) => { b.disabled = true; });
    toast('Hết giờ — hệ thống tự nộp bản nháp đã lưu.');
    route(true);
  }
}
function updateGutter() {
  const ta = $('#code'), g = $('#gutter'); if (!ta || !g) return;
  g.textContent = Array.from({ length: ta.value.split('\n').length }, (_, i) => i + 1).join('\n');
  g.scrollTop = ta.scrollTop;
}
function activeCodeSession(session) { return C === session && session.owner === S.me?.username && session.editor?.isConnected; }
function setSaveState(txt, bad = false, session = C) {
  if (!activeCodeSession(session)) return;
  const s = $('#save-state'); if (s) { s.textContent = txt; s.style.color = bad ? '#e57373' : ''; }
}
function saveDraft(session = C) {
  clearTimeout(session.saveTimer); session.saveTimer = null;
  if (session.resumeSave) { const resume = session.resumeSave; session.resumeSave = null; resume(); }
  if (session.savePromise) return session.savePromise;
  if (!session.editable || session.owner !== S.me?.username) return Promise.resolve(!session.dirty);
  if (session.source === session.savedSource && session.language === session.savedLanguage) {
    session.dirty = false;
    setSaveState(session.savedAt ? `Đã lưu lúc ${fmtTime(session.savedAt)}` : 'Đã lưu', false, session);
    return Promise.resolve(true);
  }
  session.savePromise = (async () => {
    try {
      while (session.editable && session.owner === S.me?.username && (session.source !== session.savedSource || session.language !== session.savedLanguage)) {
        const source = session.source, language = session.language;
        setSaveState('Đang lưu…', false, session);
        const r = await api('PUT', `/tournaments/${session.tid}/code`, { source, language });
        session.savedSource = source; session.savedLanguage = language; session.savedAt = r.updated_at;
        session.dirty = session.source !== source || session.language !== language;
        if (!session.dirty) setSaveState(`Đã lưu lúc ${fmtTime(r.updated_at)}`, false, session);
        if (session.dirty && session.saveTimer && session.editable && session.owner === S.me?.username) {
          await new Promise((resolve) => { session.resumeSave = resolve; });
        }
      }
      return !session.dirty;
    } catch (e) {
      session.savedSource = null; session.dirty = true;
      setSaveState(`Không lưu được: ${errMsg(e)}`, true, session);
      return false;
    } finally { session.savePromise = null; session.resumeSave = null; }
  })();
  return session.savePromise;
}
function afterCode() {
  const ta = $('#code'); if (!ta) return;
  const session = C; session.editor = ta;
  updateGutter();
  ta.addEventListener('scroll', () => { $('#gutter').scrollTop = ta.scrollTop; });
  ta.addEventListener('input', () => {
    updateGutter(); session.source = ta.value; session.revision++;
    session.dirty = session.source !== session.savedSource || session.language !== session.savedLanguage || !!session.savePromise;
    setSaveState(session.dirty ? 'Chưa lưu…' : `Đã lưu lúc ${fmtTime(session.savedAt)}`, false, session);
    clearTimeout(session.saveTimer);
    if (session.dirty) session.saveTimer = setTimeout(() => saveDraft(session), 1500);
  });
  ta.addEventListener('keydown', (e) => {
    if (e.key === 'Tab') { e.preventDefault(); ta.setRangeText('    ', ta.selectionStart, ta.selectionEnd, 'end'); ta.dispatchEvent(new Event('input')); }
  });
  $('#code-language')?.addEventListener('change', (event) => {
    if (!session.editable || !activeCodeSession(session)) return;
    session.language = event.target.value;
    $('#code-filename').textContent = session.languages.find((item) => item.id === session.language)?.filename || 'bot';
    $('#compile-log').textContent = 'Đã đổi ngôn ngữ. Mã hiện tại được giữ lại. Dùng “Khôi phục mã mẫu” để lấy mẫu tương ứng.';
    ta.dispatchEvent(new Event('input'));
  });
  $('#submit-confirm')?.addEventListener('change', (e) => { $('#submit-go').disabled = !e.target.checked; });
  if (session.dirty) { setSaveState('Chưa lưu…', false, session); session.saveTimer = setTimeout(() => saveDraft(session), 1500); }
}
function resetTemplate() {
  const session = C;
  if (!session.editable || !activeCodeSession(session)) return Promise.resolve(false);
  if (session.templateRequest?.revision === session.revision) return session.templateRequest.promise;
  if (!confirm('Thay toàn bộ mã hiện tại bằng mã mẫu?')) return Promise.resolve(false);
  const editor = session.editor;
  const request = { revision: session.revision, promise: null };
  session.templateRequest = request;
  request.promise = (async () => {
    try {
      const template = await api('GET', session.language === 'cpp17' ? '/bot-template' : `/bot-template?language=${encodeURIComponent(session.language)}`);
      if (!session.editable || !activeCodeSession(session) || session.editor !== editor || session.templateRequest !== request) return false;
      if (session.revision !== request.revision) { toast('Mã đã thay đổi trong lúc tải. Chưa khôi phục mã mẫu.'); return false; }
      if (typeof template !== 'string') throw new ApiError(502, 'BAD_TEMPLATE', 'Mã mẫu trả về không đúng định dạng.');
      editor.value = template; editor.dispatchEvent(new Event('input'));
      return true;
    } catch (e) {
      if (session.editable && activeCodeSession(session) && session.templateRequest === request) toast(errMsg(e), true);
      return false;
    } finally { if (session.templateRequest === request) session.templateRequest = null; }
  })();
  return request.promise;
}
async function compileCheck() {
  const session = C;
  if (!session.editable || !activeCodeSession(session)) return;
  const btn = $('#btn-check'), log = $('#compile-log');
  btn.disabled = true; log.textContent = 'Đang biên dịch…';
  try {
    if (!await saveDraft(session)) { if (activeCodeSession(session)) log.textContent = 'Không lưu được mã nguồn. Hãy thử lại.'; return; }
    if (!session.editable || !activeCodeSession(session)) return;
    const language = session.language;
    const r = await api('POST', `/tournaments/${session.tid}/code/check`, { source: session.source, language });
    session.used = r.compile_used;
    if (!activeCodeSession(session)) return;
    $('#compile-used').textContent = `${r.compile_used}/${r.compile_limit}`;
    log.innerHTML = r.ok ? `<span class="g">✓ Biên dịch thành công (${esc(session.languages.find((item) => item.id === language)?.label || language)}). Còn ${r.compile_limit - r.compile_used} lượt biên dịch.</span>` : `<span class="e">${esc(r.message)}</span>`;
  } catch (e) { if (activeCodeSession(session)) { log.innerHTML = `<span class="e">${esc(errMsg(e))}</span>`; if (e.code === 'COMPILE_LIMIT') $('#compile-used').textContent = `${session.limit}/${session.limit}`; } }
  finally { setTimeout(() => { if (session.editable && activeCodeSession(session)) btn.disabled = false; }, 3000); }
}
async function openSubmission() {
  const session = C;
  if (session.editable && await saveDraft(session) && session.editable && activeCodeSession(session)) $('#submit-modal').hidden = false;
}
async function submitCode() {
  const session = C;
  if (!session.editable || !activeCodeSession(session)) return;
  const go = $('#submit-go'); go.disabled = true; go.textContent = 'Đang biên dịch & nộp…';
  clearTimeout(session.saveTimer); session.saveTimer = null;
  try {
    const receipt = await api('POST', `/tournaments/${session.tid}/code/submit`, { source: session.source, language: session.language, confirm: true });
    session.editable = false;
    if (!activeCodeSession(session)) return;
    toast(receipt.accepted ? 'Đã nhận và khóa nguồn. Hệ thống sẽ biên dịch trong hàng đợi.' : 'Đã nộp bài.');
    route(true);
  } catch (e) {
    if (!activeCodeSession(session)) return;
    $('#submit-modal').hidden = true; go.textContent = 'Nộp bài'; $('#submit-confirm').checked = false;
    $('#compile-log').innerHTML = `<span class="e">${esc(e.message)}</span>`;
    const r = await api('GET', `/tournaments/${session.tid}/code`).catch(() => null);
    if (!activeCodeSession(session)) return;
    if (r) {
      $('#compile-used').textContent = `${r.compile_used}/${r.compile_limit}`;
      if (r.submitted || r.pending_submission) { session.editable = false; route(true); }
      else if (r.editable && session.dirty) saveDraft(session);
    }
    toast(e.code === 'COMPILE_ERROR' ? 'Không nộp được: lỗi biên dịch' : errMsg(e), true);
  }
}

/* ================= Bot của tôi / Hồ sơ ================= */
async function pageBot() {
  if (!S.me) return needLogin();
  if (!isContestant()) return errorHTML('Tài khoản organizer không có bot.');
  const [prof, ts] = await Promise.all([api('GET', `/users/${S.me.username}`), api('GET', '/tournaments')]);
  const mine = new Map(prof.entries.map((e) => [e.tournament_id, e]));
  const cards = ts.items.filter((t) => mine.has(t.id)).map((t) => {
    const e = mine.get(t.id), done = e.compile_status === 'READY';
    const btn = t.status === 'CODING' ? `<a class="button green" href="#/code/${t.id}">⌨ ${done ? 'Xem bài đã nộp' : 'Viết & nộp bot'}</a>` : `<a class="button" href="#/code/${t.id}">${done ? 'Xem bài đã nộp' : 'Mã mẫu'}</a>`;
    return `<section class="box"><div class="box__top"><h2>${esc(t.name)}</h2>${tag(t.status)}${btn}</div>
      <div class="stat-list" style="padding:1rem 1.2rem"><div><span>Bài nộp</span><b>${done ? '🔒 Đã nộp (không thể sửa)' : 'Chưa nộp'}</b></div>${t.status === 'CODING' ? `<div><span>Hạn nộp</span><b>${fmtTime(t.coding_deadline)}</b></div>` : ''}${e.rank ? `<div><span>Hạng hiện tại</span><b>#${e.rank} · ${e.points} điểm</b></div>` : ''}</div></section>`;
  }).join('');
  const open = ts.items.filter((t) => t.status === 'REGISTRATION_OPEN' && !mine.has(t.id));
  return `<div class="page"><div class="stack" style="max-width:62rem;margin:auto"><section class="box"><div class="box__top"><h1>Bot của tôi</h1></div>
    <p class="box-pad" style="margin:0">Bot được viết ngay trên web trong giai đoạn viết bot của giải, có đồng hồ đếm ngược. Bài nộp là bản cuối cùng và không thể thay đổi.</p></section>
    ${cards || '<div class="box box-pad dim">Bạn chưa đăng ký giải nào.</div>'}
    ${open.length ? `<section class="box"><div class="box__title">Giải đang mở đăng ký</div>${open.map((t) => `<div class="tour-row"><span class="ico">♞</span><span class="tn">${esc(t.name)}</span><span class="tp"><button class="button green small" data-act="register" data-tid="${t.id}">Đăng ký</button></span></div>`).join('')}</section>` : ''}</div></div>`;
}
async function pageUser(name) {
  const p = await api('GET', `/users/${name}`);
  const o = { win: ['1–0', 'delta-up'], loss: ['0–1', 'delta-down'], draw: ['½–½', ''] };
  return `<div class="page"><div class="lobby" style="grid-template-columns:minmax(260px,340px) minmax(0,1fr)"><div class="lobby__col"><section class="box"><div class="profile-head"><div class="avatar">${esc(p.username.slice(0, 2).toUpperCase())}</div><div><h1 style="font-size:1.6rem"><span class="online"></span> ${p.role === 'CONTESTANT' ? '<span class="title-tag">BOT</span>' : '<span class="tag warn">ORGANIZER</span> '}${esc(p.username)}</h1><div class="dim">Tham gia ${fmtDate(p.created_at)}</div></div></div>
      <div class="perf-grid"><div><b>${p.totals.points}</b><small>Điểm</small></div><div><b>${p.totals.wins}</b><small>Thắng</small></div><div><b>${p.totals.draws}</b><small>Hòa</small></div><div><b>${p.totals.losses}</b><small>Thua</small></div><div><b>${p.entries.length}</b><small>Giải</small></div><div><b>${p.games.length}</b><small>Ván gần đây</small></div></div></section></div>
    <div class="lobby__col"><section class="box"><div class="box__title">Giải tham gia</div><table class="slist"><tbody>${p.entries.map((e) => `<tr><td><a href="#/tournament/${e.tournament_id}" class="user-name">${esc(e.tournament_name)}</a></td><td>${tag(e.status)}</td><td class="r">${e.rank ? `#${e.rank} · ${e.points} điểm` : '—'}</td></tr>`).join('') || '<tr><td class="dim">Chưa tham gia giải nào.</td></tr>'}</tbody></table></section>
      <section class="box"><div class="box__title">Ván gần đây</div><table class="slist"><tbody>${p.games.map((g) => `<tr><td><a href="#/game/${g.id}">${esc(g.tournament_name)}</a></td><td>vs <span class="user-name">${esc(g.opponent)}</span> <span class="dim">(${g.color === 'white' ? 'Trắng' : 'Đen'})</span></td><td class="${g.outcome ? o[g.outcome][1] : ''}">${g.outcome ? o[g.outcome][0] : `<span class="tag live">${g.status}</span>`}</td><td class="dim">${REASONS[g.reason] || ''}</td></tr>`).join('') || '<tr><td class="dim">Chưa có ván nào.</td></tr>'}</tbody></table></section></div></div></div>`;
}

/* ================= Đăng nhập / Luật ================= */
function pageAuth(mode) {
  const reg = mode === 'register';
  return `<div class="page"><section class="box login-box"><div class="box__top"><h1>${reg ? 'Đăng ký' : 'Đăng nhập'}</h1></div><form class="box-pad" data-form="${mode}">
    <div class="form-group"><label for="u">${reg ? 'Tên đăng nhập' : 'Tên đăng nhập hoặc email'}</label><input id="u" class="form-control" autocomplete="username" required minlength="${reg ? 3 : 1}" maxlength="${reg ? 32 : 254}" ${reg ? 'pattern="[A-Za-z0-9_]+"' : ''}></div>
    ${reg ? '<div class="form-group"><label for="email">Email</label><input id="email" type="email" class="form-control" autocomplete="email" required maxlength="254"></div>' : ''}
    <div class="form-group"><label for="p">Mật khẩu</label><input id="p" type="password" class="form-control" autocomplete="${reg ? 'new-password' : 'current-password'}" required minlength="${reg ? 10 : 1}" maxlength="128">${reg ? '<div class="form-help">Tối thiểu 10 ký tự.</div>' : ''}</div>
    <div id="auth-err" class="form-help" aria-live="polite" style="color:#e57373;min-height:1.2rem"></div>
    <button class="button green big" style="width:100%" type="submit">${reg ? 'Tạo tài khoản' : 'Đăng nhập'}</button>
    ${reg ? '<p class="form-help">Cần xác minh email trước khi đăng nhập.</p>' : ''}
    <p class="form-help" style="text-align:center;margin-top:1rem">${reg ? 'Đã có tài khoản? <a href="#/login">Đăng nhập</a>' : 'Chưa có tài khoản? <a href="#/register">Đăng ký</a>'}</p></form></section></div>`;
}
function pageVerify(token) {
  if (!token || !/^[A-Za-z0-9_-]{32,128}$/.test(token)) return errorHTML('Liên kết xác minh không hợp lệ.');
  return `<div class="page"><section class="box login-box"><div class="box__top"><h1>Xác minh email</h1></div><div class="box-pad"><p>Chọn xác minh để kích hoạt tài khoản.</p><button class="button green big" data-act="verify-email" data-token="${esc(token)}">Xác minh email</button><p class="form-help"><a href="#/login">Đăng nhập</a></p></div></section></div>`;
}
function pageRules() {
  return `<div class="page"><section class="box box-pad" style="max-width:56rem;margin:auto"><h1 style="font-size:1.8rem;margin-bottom:1rem">Luật OTT v1</h1>
    <p>Bàn 9×9, mỗi bên 10 quân: 3 Kéo, 3 Búa, 3 Bao, 1 Vua. Trắng đặt quân trước ở hàng 1–2, Đen ở hàng 8–9; vị trí được giấu tới khi bắt đầu đi quân.</p>
    <div class="row" style="gap:2rem;flex-wrap:wrap;margin:1rem 0">${['k', 'b', 'a', 'v'].map((t) => `<span class="row"><svg viewBox="0 0 100 100" width="48" height="48"><use href="#pc-w${t}"/></svg><svg viewBox="0 0 100 100" width="48" height="48"><use href="#pc-b${t}"/></svg><span><b class="user-name">${PNAME[t]}</b><div class="dim">${{ k: 'bắt Bao', b: 'bắt Kéo', a: 'bắt Búa', v: 'đi 1 ô mọi hướng, không bắt' }[t]}</div></span></span>`).join('')}</div>
    <ul><li>Quân thường đi 1 ô: tiến, chéo tiến, chéo lùi; chỉ bắt khi tiến hoặc chéo tiến.</li><li>Tới hàng cuối: phong cấp Xe (trượt hàng/cột) hoặc Tượng (trượt chéo), thêm 1 ô mọi hướng.</li><li>Bắt Vua hoặc loại hết quân: thắng. Hết giờ, action sai hoặc bot crash: thua. Lặp thế 3 lần hoặc 500 nước: hòa.</li><li>Thắng 3 điểm, hòa 1, thua 0. Vòng tròn hai lượt, đổi màu mỗi cặp.</li><li>Viết bot trên web trong thời gian quy định (mặc định 60 phút), tối đa 10 lượt biên dịch, nộp một lần và không thể sửa.</li></ul></section></div>`;
}

/* ================= Organizer ================= */
const orgGuard = () => (S.me ? (isOrg() ? null : errorHTML('Chỉ organizer được vào trang này.')) : needLogin());
async function pageOrgList() {
  const g = orgGuard(); if (g) return g;
  const ts = (await api('GET', '/tournaments')).items;
  return `<div class="page"><div class="stack" style="max-width:62rem;margin:auto"><section class="box"><div class="box__top"><h1>Quản trị giải</h1><span class="tag warn">ORGANIZER</span><a class="button green" href="#/organizer/new">＋ Tạo giải</a></div>
    <table class="slist"><thead><tr><th>Giải</th><th>Trạng thái</th><th>Suất</th><th>Đồng hồ</th><th></th></tr></thead><tbody>${ts.map((t) => `<tr><td class="user-name">${esc(t.name)}</td><td>${tag(t.status)}</td><td>${t.entries_count}</td><td>${clockLabel(t.clock_seconds)}</td><td class="r"><a href="#/organizer/manage/${t.id}">Quản lý »</a></td></tr>`).join('') || '<tr><td colspan="5" class="dim">Chưa có giải nào.</td></tr>'}</tbody></table></section></div></div>`;
}
function pageOrgNew() {
  const g = orgGuard(); if (g) return g;
  return `<div class="page"><div class="lobby" style="grid-template-columns:minmax(0,1fr) minmax(260px,340px)"><section class="box"><div class="box__top"><h1>Tạo giải đấu</h1></div>
    <form class="box-pad" data-form="new-tour"><div class="form-group"><label for="tn">Tên giải</label><input id="tn" class="form-control" value="OTT Open Cup" maxlength="80" required></div>
      <div class="form-split"><div class="form-group"><label>Game</label><select class="form-control" disabled><option>OTT v1 (9×9)</option></select></div><div class="form-group"><label>Ngôn ngữ bot</label><select class="form-control" disabled><option>C++17 · C17 · Rust 2021 · Go</option></select></div></div>
      <div class="form-group"><label>Đồng hồ mỗi bên (khi thi đấu)</label><div class="radio-row"><label><input type="radio" name="clk" value="300"><span>5 phút</span></label><label><input type="radio" name="clk" value="600" checked><span>10 phút</span></label><label><input type="radio" name="clk" value="3600"><span>1 giờ</span></label></div><div class="form-help">Đồng hồ tổng, gồm cả giai đoạn xếp quân. Không đổi được sau khi khóa.</div></div>
      <div class="form-group"><label for="cm">Thời gian viết bot (phút)</label><input id="cm" type="number" class="form-control" min="1" max="240" value="60" required><div class="form-help">Đồng hồ đếm ngược bắt đầu khi bạn bấm “Bắt đầu giai đoạn viết bot”. Hết giờ, hệ thống tự nộp bản nháp đã lưu của đội chưa nộp.</div></div>
      <div id="form-err" class="form-help" style="color:#e57373;min-height:1.2rem"></div>
      <div class="form-actions"><a class="button" href="#/organizer">Hủy</a><button class="button green big" type="submit">Tạo giải</button></div></form></section>
    <section class="box"><div class="box__title">Quy trình</div><div class="stat-list"><div><span>1</span><b>Nháp → Mở đăng ký</b></div><div><span>2</span><b>Bắt đầu viết bot (đếm ngược)</b></div><div><span>3</span><b>Khóa roster</b></div><div><span>4</span><b>Sinh lịch · Bắt đầu giải</b></div></div></section></div></div>`;
}
const ORG_STEPS = [['DRAFT', 'Nháp'], ['REGISTRATION_OPEN', 'Đăng ký'], ['CODING', 'Viết bot'], ['LOCKED', 'Đã khóa'], ['RUNNING', 'Thi đấu'], ['FINISHED', 'Kết thúc']];
async function pageOrgManage(id, q) {
  const g = orgGuard(); if (g) return g;
  const renderId = routing;
  const offset = Math.max(0, Number.parseInt(q.get('offset') || '0', 10) || 0);
  const scheduleParams = new URLSearchParams({ pagination: 'keyset', limit: '100', offset: String(offset) });
  const scheduleCursor = q.get('cursor');
  if (scheduleCursor) {
    scheduleParams.set('cursor', scheduleCursor);
    scheduleParams.set('direction', q.get('direction') || 'after');
  }
  const [t, ent, sched] = await Promise.all([api('GET', `/tournaments/${id}`), api('GET', `/tournaments/${id}/entries`), api('GET', `/tournaments/${id}/schedule?${scheduleParams}`)]);
  if (renderId !== routing) return '';
  syncOffset(t.server_time);
  const idx = ORG_STEPS.findIndex(([s]) => s === t.status);
  const btn = (op, label, cls = 'green') => `<button class="button ${cls}" data-act="lifecycle" data-op="${op}" data-tid="${id}">${label}</button>`;
  const actions = {
    DRAFT: btn('open-registration', 'Mở đăng ký'),
    REGISTRATION_OPEN: btn('start-coding', `Bắt đầu giai đoạn viết bot (${t.coding_seconds / 60} phút)`),
    CODING: btn('lock', 'Khóa roster'),
    LOCKED: t.schedule_created ? btn('start', 'Bắt đầu giải') : btn('schedule', 'Sinh lịch vòng tròn hai lượt'),
    RUNNING: btn('resume', 'Tiếp tục trận tạm dừng', ''),
  }[t.status] || '';
  const canCancel = ['DRAFT', 'REGISTRATION_OPEN', 'CODING', 'LOCKED'].includes(t.status);
  const submitted = ent.items.filter((e) => e.submitted_at).length;
  if (t.status === 'CODING') every(500, () => { const el = $('#org-timer'); if (el) el.textContent = fmtCountdown(t.coding_deadline - nowSrv()); });
  if (['CODING', 'RUNNING', 'REGISTRATION_OPEN', 'LOCKED'].includes(t.status)) every(10000, () => {
    if (renderId === routing && location.hash.startsWith(`#/organizer/manage/${id}`) && !LIVE.ok) route(true);
  });
  const entryState = (e) => e.submitted_at ? '<span class="tag ok">🔒 Đã nộp</span>' : e.submission_status === 'QUEUED' ? '<span class="tag warn">Đã nhận, chờ biên dịch</span>' : e.submission_status === 'REVIEW' ? '<span class="tag err">Cần kiểm tra hạ tầng</span>' : e.submit_note ? `<span class="tag err" title="${esc(e.submit_note)}">Không nộp được</span>` : t.status === 'CODING' ? '<span class="tag warn">Đang viết</span>' : '<span class="tag">Chưa nộp</span>';
  return `<div class="page"><div class="stack"><section class="box"><div class="box__top"><h1>Quản lý: ${esc(t.name)}</h1><span class="tag warn">ORGANIZER</span>${tag(t.status)}</div>
    <div class="stepper">${ORG_STEPS.map(([s, n], i) => `<div class="step ${t.status === 'CANCELLED' ? '' : i < idx ? 'done' : i === idx ? 'cur' : ''}"><i>${i < idx ? '✓' : i + 1}</i>${n}</div>`).join('')}</div>
    ${t.status === 'CODING' ? `<div class="row" style="padding:0 1.3rem 1rem"><span class="dim">Hạn nộp bot:</span><b class="mono" id="org-timer" style="font-size:1.4rem;color:var(--text-hi)">${fmtCountdown(t.coding_deadline - nowSrv())}</b><span class="dim">· ${submitted}/${ent.items.length} đội đã nộp</span></div>` : ''}
    <div class="row" style="padding:0 1.3rem 1.2rem;flex-wrap:wrap">${actions}${canCancel ? btn('cancel', 'Hủy giải', 'red') : ''}<span class="spacer"></span><span class="dim">Sau khi khóa: không sửa luật, đồng hồ, suất hoặc bot.</span></div></section>
    <div class="lobby" style="grid-template-columns:minmax(0,1.2fr) minmax(0,1fr)">
      <section class="box"><div class="box__title">Danh sách suất <span class="dim">Organizer không xem source bot</span></div><table class="slist"><thead><tr><th>#</th><th>Thí sinh</th><th>Bài nộp</th><th>Biên dịch</th><th></th></tr></thead><tbody>
        ${ent.items.map((e, i) => `<tr><td class="rankn">${i + 1}</td><td class="user-name">${esc(e.username)}</td><td>${entryState(e)}</td><td>${e.compile_status ? `<span class="tag ${e.compile_status === 'READY' ? 'ok' : 'err'}">${e.compile_status === 'READY' ? 'Thành công' : 'Lỗi'}</span>` : '—'}</td><td class="r">${['REGISTRATION_OPEN', 'CODING'].includes(t.status) ? `<button class="button small red" data-act="remove-entry" data-eid="${e.id}">Loại</button>` : ''}</td></tr>`).join('') || '<tr><td colspan="5" class="dim">Chưa có thí sinh đăng ký.</td></tr>'}</tbody></table></section>
      <section class="box"><div class="box__title">Lịch & hàng đợi trận <span class="dim">${t.matches_finished}/${t.matches_total}</span></div><table class="slist"><tbody>
        ${sched.matches.map((m) => `<tr><td class="dim">L${m.leg_no}·V${m.round_no}</td><td>${esc(m.white.username)} – ${esc(m.black.username)}</td><td>${m.status === 'FINISHED' ? SCORE[m.result] : m.status === 'RUNNING' ? '<span class="tag live">LIVE</span>' : m.status === 'PAUSED' ? '<span class="tag err">Tạm dừng</span>' : '<span class="tag info">Chờ</span>'}</td><td class="r">${m.status === 'SCHEDULED' ? '' : `<a href="#/game/${m.id}">Xem</a>`}</td></tr>`).join('') || '<tr><td class="dim">Chưa sinh lịch.</td></tr>'}</tbody></table>
        ${sched.total ? `<div class="row" style="padding:.8rem;justify-content:center;gap:.75rem"><a class="button small ${sched.previous_cursor ? '' : 'disabled'}" href="#/organizer/manage/${id}?offset=${Math.max(0, offset - sched.limit)}&cursor=${encodeURIComponent(sched.previous_cursor || '')}&direction=before">Trước</a><span class="dim">${offset + 1}–${Math.min(offset + sched.matches.length, sched.total)} / ${sched.total}</span><a class="button small ${sched.next_cursor ? '' : 'disabled'}" href="#/organizer/manage/${id}?offset=${offset + sched.limit}&cursor=${encodeURIComponent(sched.next_cursor || '')}&direction=after">Tiếp</a></div>` : ''}</section></div></div></div>`;
}

/* ================= Cập nhật trực tiếp (SSE) ================= */
const PAGE = { key: '', id: null, sub: null, waiting: false };
const LIVE = { ok: false, es: null, timer: null };
function setLiveStatus(ok) {
  const el = $('#live-status'); if (!el) return;
  el.classList.toggle('on', ok);
  el.textContent = ok ? '● Trực tiếp' : '○ Đang kết nối lại…';
  el.title = ok ? 'Máy chủ đẩy cập nhật ngay khi có thay đổi' : 'Mất kết nối cập nhật trực tiếp; trang vẫn tự làm mới định kỳ';
}
// Throttle (không debounce): một lần làm mới đang chờ thì không đặt lại giờ, tránh bị bỏ đói khi sự kiện đến liên tục.
function softRefresh(ms = 300) { if (LIVE.timer) return; LIVE.timer = setTimeout(() => { LIVE.timer = null; route(true); }, ms); }
function onLiveEvent(ev) {
  const k = PAGE.key, tid = ev.tid || ev.id;
  if (k === 'game' || (k === 'watch' && PAGE.id)) {
    if (G.refresh && (ev.type === 'hello' || (ev.type === 'match' && ev.id === PAGE.id))) G.refresh();
    return;
  }
  if (k === 'code') { if (PAGE.waiting && (ev.type === 'hello' || (ev.type === 'tournament' && ev.id === PAGE.id))) softRefresh(200); return; }
  if (k === 'organizer' && PAGE.id === 'new') return;
  if (k === 'login' || k === 'register' || k === 'rules') return;
  if (ev.type === 'hello') { softRefresh(0); return; }
  const slow = ev.type === 'match' && ev.move_count !== undefined;
  if (k === 'tournament') {
    if (tid !== PAGE.id) return;
    if (ev.type === 'match' && ev.move_count !== undefined) return; // standings change only when match status changes
    softRefresh(300); return;
  }
  if (k === 'organizer' && PAGE.id === 'manage') {
    if (tid !== PAGE.sub) return;
    if (ev.type === 'match' && ev.move_count !== undefined) return;
    softRefresh(300); return;
  }
  if (slow) {
    if (k === '' || (k === 'watch' && !PAGE.id)) refreshLiveWidgets();
    return;
  }
  softRefresh(300);
}
function connectStream() {
  if (TUNNEL) { // Quick Tunnel không hỗ trợ SSE: dùng polling định kỳ
    const el = $('#live-status');
    if (el) { el.textContent = `◔ Cập nhật mỗi ${Math.round(CFG.pollMs / 1000)} giây`; el.title = 'Chế độ demo qua Cloudflare Tunnel: trang tự làm mới định kỳ, không có kênh đẩy trực tiếp'; }
    return;
  }
  if (!window.EventSource) return;
  const es = new EventSource('/api/v1/stream');
  LIVE.es = es;
  es.addEventListener('hello', () => { LIVE.ok = true; setLiveStatus(true); onLiveEvent({ type: 'hello' }); });
  es.onmessage = (e) => { LIVE.ok = true; try { onLiveEvent(JSON.parse(e.data)); } catch { /* bỏ qua */ } };
  es.onerror = () => { LIVE.ok = false; setLiveStatus(false); }; // EventSource tự nối lại
}

/* ================= Router ================= */
let routing = 0;
async function route(soft = false) {
  const my = ++routing;
  const raw = location.hash.slice(1) || '/';
  const [path, qs] = raw.split('?');
  const q = new URLSearchParams(qs || '');
  const seg = path.split('/').filter(Boolean);
  const key = seg[0] || '';
  clearTimers();
  PAGE.key = key; PAGE.id = seg[1] || null; PAGE.sub = seg[2] || null; PAGE.waiting = false; G.refresh = null;
  renderHeader(key);
  const scroll = window.scrollY;
  if (!soft) $('#main').innerHTML = loadingHTML;
  let html;
  try {
    switch (key) {
      case '': html = await pageHome(); break;
      case 'tournaments': html = await pageTournaments(); break;
      case 'tournament': html = await pageTournament(seg[1], q); break;
      case 'game': case 'watch': html = seg[1] ? await pageGame(seg[1]) : await pageWatch(); break;
      case 'code': html = await pageCode(seg[1]); break;
      case 'bot': html = await pageBot(); break;
      case 'user': html = await pageUser(seg[1]); break;
      case 'login': case 'register': html = pageAuth(key); break;
      case 'verify': html = pageVerify(seg[1]); break;
      case 'rules': html = pageRules(); break;
      case 'organizer': html = seg[1] === 'new' ? pageOrgNew() : seg[1] === 'manage' ? await pageOrgManage(seg[2], q) : await pageOrgList(); break;
      default: html = await pageHome();
    }
  } catch (e) { html = e instanceof ApiError && e.status === 401 ? needLogin() : errorHTML(errMsg(e)); }
  if (my !== routing) return;
  $('#main').innerHTML = html;
  if (key === 'code') afterCode();
  if (key === 'game' || key === 'watch') { if (seg[1]) paintGame(); }
  window.scrollTo(0, soft ? scroll : 0);
}

/* ================= Sự kiện ================= */
async function run(fn, okMsg) {
  try { await fn(); if (okMsg) toast(okMsg); } catch (e) { toast(errMsg(e), true); }
}
document.addEventListener('click', (e) => {
  const t = e.target;
  const tab = t.closest('[data-tab-group]');
  if (tab) {
    const grp = tab.dataset.tabGroup;
    $$(`[data-tab-group="${grp}"]`).forEach((b) => b.classList.toggle('on', b === tab));
    $$(`[data-pane^="${grp}:"]`).forEach((p) => p.classList.toggle('on', p.dataset.pane === `${grp}:${tab.dataset.tab}`));
    return;
  }
  const nav = t.closest('[data-nav]');
  if (nav && G.m) {
    const last = G.positions.length - 1, a = nav.dataset.nav;
    if (a === 'first') G.ply = 0; else if (a === 'prev') G.ply = Math.max(0, G.ply - 1); else if (a === 'next') G.ply = Math.min(last, G.ply + 1);
    else if (a === 'last') G.ply = last; else if (a === 'flip') G.flip = !G.flip;
    paintGame(); return;
  }
  const mv = t.closest('.mv'); if (mv) { G.ply = +mv.dataset.ply; paintGame(); return; }
  const act = t.closest('[data-act]'); if (!act) return;
  const tid = act.dataset.tid;
  switch (act.dataset.act) {
    case 'logout': e.preventDefault(); run(async () => { await api('POST', '/auth/logout'); S.me = null; S.csrf = null; location.hash = '#/login'; await route(); }); break;
    case 'verify-email': run(async () => { await api('POST', '/auth/verify-email', { token: act.dataset.token }); location.hash = '#/login'; await route(); }, 'Email đã xác minh. Đăng nhập để tiếp tục.'); break;
    case 'register': run(async () => { await api('POST', `/tournaments/${tid}/entry`); route(true); }, 'Đã đăng ký suất dự thi'); break;
    case 'lifecycle': {
      const op = act.dataset.op;
      if (op === 'cancel' && !confirm('Hủy giải này? Không thể hoàn tác.')) break;
      if (op === 'start-coding' && !confirm('Bắt đầu giai đoạn viết bot? Đồng hồ đếm ngược chạy ngay và đóng đăng ký.')) break;
      run(async () => { await api('POST', `/tournaments/${tid}/${op}`); route(true); }, 'Đã cập nhật giải'); break;
    }
    case 'remove-entry': if (confirm('Loại thí sinh này khỏi giải?')) run(async () => { await api('DELETE', `/entries/${act.dataset.eid}`); route(true); }, 'Đã loại thí sinh'); break;
    case 'compile-check': compileCheck(); break;
    case 'reset-template': resetTemplate(); break;
    case 'submit-open': openSubmission(); break;
    case 'submit-cancel': $('#submit-modal').hidden = true; break;
    case 'submit-go': submitCode(); break;
    default: break;
  }
});
document.addEventListener('submit', async (e) => {
  const form = e.target.closest('[data-form]'); if (!form) return;
  e.preventDefault();
  const kind = form.dataset.form;
  if (kind === 'login' || kind === 'register') {
    const username = $('#u').value.trim(), password = $('#p').value, err = $('#auth-err');
    err.style.color = '#e57373';
    err.textContent = '';
    try {
      if (kind === 'register') {
        await api('POST', '/auth/register', { username, email: $('#email').value.trim(), password });
        err.style.color = '#64c78a';
        err.textContent = 'Đã gửi email xác minh. Mở hộp thư để kích hoạt tài khoản.';
        return;
      }
      const r = await api('POST', '/auth/login', { username, password });
      S.csrf = r.csrf_token; S.me = r.user;
      location.hash = '#/'; await route();
    } catch (ex) { err.textContent = errMsg(ex); }
  } else if (kind === 'new-tour') {
    try {
      const t = await api('POST', '/tournaments', { name: $('#tn').value.trim(), clock_seconds: +$('input[name=clk]:checked').value, coding_minutes: +$('#cm').value });
      toast('Đã tạo giải'); location.hash = `#/organizer/manage/${t.id}`;
    } catch (ex) { $('#form-err').textContent = errMsg(ex); }
  }
});
document.addEventListener('keydown', (e) => {
  if (e.target.matches('input, textarea, select') || !G.m || !$('.round')) return;
  const map = { ArrowLeft: 'prev', ArrowRight: 'next', ArrowUp: 'first', ArrowDown: 'last', f: 'flip' };
  if (map[e.key]) { document.querySelector(`[data-nav="${map[e.key]}"]`)?.click(); e.preventDefault(); }
});
window.addEventListener('hashchange', () => route());
(async () => { await loadMe(); await route(); connectStream(); })();
