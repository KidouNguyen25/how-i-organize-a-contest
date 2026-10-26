// OTT Arena — theme giao diện, theme bàn cờ, bộ quân, hiệu ứng nước đi, âm thanh và vài tiện ích UX.
// Script thường (không module) nạp trong <head> để áp theme trước khi vẽ trang, tránh nháy màu.
// Không đụng app.js: nhận biết nước đi bằng cách quan sát DOM của #board-wrap.
(() => {
  'use strict';
  const KEY = 'ott.prefs.v1';
  const root = document.documentElement;
  const reduceMq = window.matchMedia ? matchMedia('(prefers-reduced-motion: reduce)') : { matches: false };

  /* ================= Tùy chọn ================= */
  const OPT = {
    ui: [['dark', 'Tối'], ['midnight', 'Nửa đêm'], ['amoled', 'AMOLED'], ['forest', 'Rừng'], ['light', 'Sáng']],
    accent: [['blue', 'Xanh dương'], ['teal', 'Ngọc'], ['orange', 'Cam'], ['pink', 'Hồng'], ['violet', 'Tím']],
    board: [['classic', 'Cổ điển'], ['green', 'Xanh lá'], ['blue', 'Biển'], ['violet', 'Tím'], ['sakura', 'Anh đào'], ['wood', 'Gỗ'], ['marble', 'Cẩm thạch'],
      ['paper', 'Giấy'], ['ice', 'Băng'], ['neon', 'Neon'], ['matrix', 'Matrix']],
    pieces: [['classic', 'Cổ điển'], ['flat', 'Tương phản'], ['wood', 'Gỗ'], ['neon', 'Neon'], ['emoji', 'Emoji']],
    last: [['fill', 'Tô ô'], ['ring', 'Viền'], ['glow', 'Phát sáng'], ['none', 'Tắt']],
    radius: [['sm', 'Vuông'], ['md', 'Bo vừa'], ['lg', 'Bo tròn']],
    pscale: [['80', 'Nhỏ'], ['90', 'Vừa'], ['94', 'Lớn'], ['100', 'Đầy ô']],
    anim: [['off', 'Tắt'], ['fast', 'Nhanh'], ['normal', 'Vừa'], ['slow', 'Chậm']],
    capfx: [['off', 'Tắt'], ['burst', 'Hạt'], ['emoji', 'Biểu tượng'], ['shock', 'Sóng']],
  };
  const DEFAULTS = { ui: 'dark', accent: 'blue', board: 'classic', pieces: 'classic', last: 'fill', radius: 'md', pscale: '94', coords: true,
    anim: 'normal', capfx: 'emoji', shake: true, win: true, sound: false, vol: 50 };
  const ANIM_MS = { off: 0, fast: 140, normal: 240, slow: 420 };

  function load() {
    const P = { ...DEFAULTS };
    let raw = {};
    try { raw = JSON.parse(localStorage.getItem(KEY) || '{}') || {}; } catch { raw = {}; }
    if (!Object.keys(raw).length && reduceMq.matches) Object.assign(P, { anim: 'off', capfx: 'off', shake: false, win: false });
    for (const k of Object.keys(DEFAULTS)) {
      if (!(k in raw)) continue;
      const v = raw[k];
      if (OPT[k]) { if (OPT[k].some(([id]) => id === String(v))) P[k] = String(v); }
      else if (typeof DEFAULTS[k] === 'boolean') P[k] = !!v;
      else if (k === 'vol') P[k] = Math.min(100, Math.max(0, +v || 0));
    }
    return P;
  }
  const P = load();
  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(P)); } catch { /* bỏ qua: private mode */ } };

  function applyAttrs() {
    root.dataset.ui = P.ui; root.dataset.accent = P.accent; root.dataset.board = P.board; root.dataset.pieces = P.pieces;
    root.dataset.last = P.last; root.dataset.radius = P.radius; root.dataset.coords = P.coords ? 'on' : 'off';
    root.style.setProperty('--piece-scale', P.pscale + '%');
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = { dark: '#262421', midnight: '#141d38', amoled: '#050505', forest: '#15251c', light: '#262421' }[P.ui] || '#262421';
  }
  applyAttrs();

  /* ================= Bộ quân ================= */
  const ICON = {
    k: (c) => `<g fill="none" stroke="${c}" stroke-width="6" stroke-linecap="round"><path d="M42 62 L64 26 M58 62 L36 26"/><circle cx="40" cy="71" r="8"/><circle cx="60" cy="71" r="8"/></g>`,
    b: (c) => `<g fill="${c}"><rect x="28" y="26" width="44" height="20" rx="4"/><rect x="44" y="42" width="12" height="36" rx="3"/></g>`,
    a: (c, d) => `<path d="M33 22 H58 L71 35 V78 H33 Z" fill="${c}"/><path d="M40 50 H64 M40 60 H64 M40 70 H55" stroke="${d}" stroke-width="4" stroke-linecap="round"/><path d="M58 22 V35 H71" fill="none" stroke="${d}" stroke-width="3"/>`,
    v: (c) => `<path d="M28 68 L24 34 L40 49 L50 26 L60 49 L76 34 L72 68 Z" fill="${c}"/><rect x="28" y="72" width="44" height="9" rx="3" fill="${c}"/>`,
    h: (c) => `<text x="50" y="68" font-size="52" font-weight="700" text-anchor="middle" fill="${c}" font-family="Segoe UI,sans-serif">?</text>`,
  };
  const EMOJI = { k: '✂️', b: '🔨', a: '📄', v: '👑', h: '❓' };
  const EMOJI_FONT = 'Segoe UI Emoji,Apple Color Emoji,Noto Color Emoji,sans-serif';
  const SETS = {
    flat: (s, t) => {
      const w = s === 'w', fill = w ? '#f7f6f2' : '#1d1c1a', ink = w ? '#1d1c1a' : '#f7f6f2', bar = w ? '#4aa3ff' : '#ff7a4a';
      return `<rect x="8" y="8" width="84" height="84" rx="20" fill="${fill}" stroke="${ink}" stroke-width="5"/><rect x="26" y="82" width="48" height="6" rx="3" fill="${bar}"/>`
        + `<g transform="translate(0 -4)${t === 'h' ? '" opacity=".85' : ''}">${ICON[t](ink, fill)}</g>`;
    },
    wood: (s, t) => {
      const w = s === 'w';
      return `<circle cx="50" cy="50" r="45" fill="url(#fx-wood-${s})" stroke="${w ? '#8a5a2b' : '#1d1008'}" stroke-width="4"/>`
        + `<circle cx="50" cy="50" r="38" fill="none" stroke="${w ? '#fff' : '#000'}" stroke-opacity=".12" stroke-width="2"/>`
        + `<g${t === 'h' ? ' opacity=".85"' : ''}>${ICON[t](w ? '#5b3a1b' : '#f0cf9a', w ? '#e3bd80' : '#4a2d17')}</g>`
        + '<ellipse cx="38" cy="26" rx="22" ry="10" fill="#fff" opacity=".16"/>';
    },
    neon: (s, t) => {
      const c = s === 'w' ? '#38e8ff' : '#ff4fd8';
      return `<circle cx="50" cy="50" r="45" fill="#0b0f1e" stroke="${c}" stroke-width="4"/><circle cx="50" cy="50" r="38" fill="none" stroke="${c}" stroke-opacity=".3" stroke-width="1.5"/>`
        + `<g${t === 'h' ? ' opacity=".85"' : ''}>${ICON[t](c, '#0b0f1e')}</g>`;
    },
    emoji: (s, t) => {
      const w = s === 'w';
      return `<circle cx="50" cy="50" r="45" fill="${w ? '#f4f1ea' : '#34312d'}" stroke="${w ? '#2b2825' : '#0d0c0b'}" stroke-width="4"/>`
        + `<text x="50" y="66" font-size="48" text-anchor="middle" font-family="${EMOJI_FONT}">${EMOJI[t]}</text>`;
    },
  };
  const SIDES = ['w', 'b'], TYPES = ['k', 'b', 'a', 'v', 'h'];
  const ORIG = {};
  function pieceMarkup(set, s, t) {
    if (set === 'classic') return ORIG[s + t] || '';
    return SETS[set](s, t);
  }
  function applyPieces() {
    for (const s of SIDES) for (const t of TYPES) {
      const el = document.getElementById(`pc-${s}${t}`);
      if (el) el.innerHTML = pieceMarkup(P.pieces, s, t);
    }
  }
  function initPieces() {
    for (const s of SIDES) for (const t of TYPES) {
      const el = document.getElementById(`pc-${s}${t}`);
      if (el) ORIG[s + t] = el.innerHTML;
    }
    const defs = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    defs.setAttribute('width', '0'); defs.setAttribute('height', '0'); defs.setAttribute('aria-hidden', 'true'); defs.style.position = 'absolute';
    defs.innerHTML = '<defs><radialGradient id="fx-wood-w" cx=".35" cy=".3" r=".9"><stop offset="0" stop-color="#f7e2b4"/><stop offset=".6" stop-color="#d9ae6c"/><stop offset="1" stop-color="#b98443"/></radialGradient>'
      + '<radialGradient id="fx-wood-b" cx=".35" cy=".3" r=".9"><stop offset="0" stop-color="#7a4f2f"/><stop offset=".6" stop-color="#4b2d18"/><stop offset="1" stop-color="#2b170a"/></radialGradient></defs>';
    document.body.prepend(defs);
    applyPieces();
  }

  /* ================= Âm thanh (Web Audio, không cần file) ================= */
  let AC = null;
  function audio() {
    if (!P.sound) return null;
    try {
      AC = AC || new (window.AudioContext || window.webkitAudioContext)();
      if (AC.state === 'suspended') AC.resume();
      return AC;
    } catch { return null; }
  }
  const unlock = () => { if (P.sound) audio(); };
  function env(ctx, g, t0, dur, peak) {
    g.gain.setValueAtTime(0.0001, t0);
    g.gain.exponentialRampToValueAtTime(Math.max(0.0002, peak), t0 + 0.005);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
  }
  function tone(ctx, freq, t0, dur, type, peak, slideTo) {
    const o = ctx.createOscillator(), g = ctx.createGain();
    o.type = type; o.frequency.setValueAtTime(freq, t0);
    if (slideTo) o.frequency.exponentialRampToValueAtTime(slideTo, t0 + dur);
    env(ctx, g, t0, dur, peak * (P.vol / 100));
    o.connect(g).connect(ctx.destination); o.start(t0); o.stop(t0 + dur + 0.02);
  }
  function noise(ctx, t0, dur, peak, hp) {
    const n = Math.floor(ctx.sampleRate * dur), buf = ctx.createBuffer(1, n, ctx.sampleRate), d = buf.getChannelData(0);
    for (let i = 0; i < n; i++) d[i] = Math.random() * 2 - 1;
    const src = ctx.createBufferSource(), f = ctx.createBiquadFilter(), g = ctx.createGain();
    src.buffer = buf; f.type = 'highpass'; f.frequency.value = hp;
    env(ctx, g, t0, dur, peak * (P.vol / 100));
    src.connect(f).connect(g).connect(ctx.destination); src.start(t0);
  }
  const SND = {
    move() { const c = audio(); if (!c) return; const t = c.currentTime; noise(c, t, 0.04, 0.5, 1800); tone(c, 210, t, 0.09, 'sine', 0.5, 120); },
    capture() { const c = audio(); if (!c) return; const t = c.currentTime; noise(c, t, 0.11, 0.8, 900); tone(c, 150, t, 0.2, 'triangle', 0.8, 70); tone(c, 520, t, 0.06, 'square', 0.12); },
    end() { const c = audio(); if (!c) return; const t = c.currentTime; [523, 659, 784, 1047].forEach((f, i) => tone(c, f, t + i * 0.11, 0.5, 'triangle', 0.35)); },
    tick() { const c = audio(); if (!c) return; tone(c, 880, c.currentTime, 0.05, 'sine', 0.3); },
  };

  /* ================= Hiệu ứng nước đi ================= */
  const FILES = 'abcdefghi';
  const animOn = () => ANIM_MS[P.anim] > 0 && !document.hidden;
  // snapshot: đọc DOM của một .cg-board 9x9 (có tọa độ) -> ô nào có quân gì, ô nào là nước đi cuối, bàn đang xoay hay không
  function snapshot(board) {
    const sq = board.children;
    if (sq.length !== 81) return null;
    const file = sq[72].querySelector('.coord.file');
    const flip = !!file && file.textContent === 'i';
    const cells = new Array(81);
    for (let i = 0; i < 81; i++) { const u = sq[i].querySelector('use'); cells[i] = u ? u.getAttribute('href') : ''; }
    return { cells, flip, el: board };
  }
  function sqName(i, flip) { const r = Math.floor(i / 9), c = i % 9; return flip ? FILES[8 - c] + (r + 1) : FILES[c] + (9 - r); }
  // đúng hai ô thay đổi và một quân dịch chuyển từ ô này sang ô kia (cả khi bấm ◀ lùi nước)
  function diffMove(prev, cur) {
    if (prev.flip !== cur.flip) return null;
    const d = [];
    for (let i = 0; i < 81; i++) if (prev.cells[i] !== cur.cells[i]) { d.push(i); if (d.length > 2) return null; }
    if (d.length !== 2) return null;
    for (const [src, dst] of [[d[0], d[1]], [d[1], d[0]]]) {
      const p = prev.cells[src];
      if (p && cur.cells[dst] === p && prev.cells[dst] !== p) {
        return { src, dst, mover: p, victim: prev.cells[dst] || '', restored: cur.cells[src] || '' };
      }
    }
    return null;
  }
  const typeOf = (href) => (href || '').slice(-1);
  const sideOf = (href) => (href || '').slice(-2, -1);

  function layer(board) {
    let l = board.querySelector(':scope > .fx-layer');
    if (!l) { l = document.createElement('div'); l.className = 'fx-layer'; board.appendChild(l); }
    return l;
  }
  function boxOf(board, i) {
    const s = board.children[i];
    return { x: s.offsetLeft, y: s.offsetTop, w: s.offsetWidth, h: s.offsetHeight, el: s };
  }
  const rand = (a, b) => a + Math.random() * (b - a);
  function spawn(l, cls, css, kf, opts) {
    const n = document.createElement('div'); n.className = cls;
    Object.assign(n.style, css); l.appendChild(n);
    const a = n.animate(kf, opts); a.onfinish = a.oncancel = () => n.remove();
    return n;
  }
  function slide(board, mv, ms) {
    const a = boxOf(board, mv.src), b = boxOf(board, mv.dst);
    const piece = b.el.querySelector('.piece');
    if (!piece || !ms) return;
    const dx = a.x - b.x, dy = a.y - b.y;
    b.el.style.zIndex = '5';
    const an = piece.animate([
      { transform: `translate(${dx}px, ${dy}px) scale(1.14)`, filter: 'drop-shadow(0 8px 6px rgba(0,0,0,.45))' },
      { transform: 'translate(0, 0) scale(1)', filter: 'drop-shadow(0 1px 1px rgba(0,0,0,.35))' },
    ], { duration: ms, easing: 'cubic-bezier(.25,.9,.3,1)' });
    an.onfinish = an.oncancel = () => { b.el.style.zIndex = ''; };
  }
  function captureFx(board, mv, big) {
    const kind = P.capfx, b = boxOf(board, mv.dst), l = layer(board);
    const cx = b.x + b.w / 2, cy = b.y + b.h / 2, u = b.w;
    const victimSide = sideOf(mv.victim), moverType = typeOf(mv.mover);
    if (kind !== 'off') {
      // bóng quân bị ăn mờ dần + xoay
      spawn(l, 'fx-ghost', { left: b.x + 'px', top: b.y + 'px', width: b.w + 'px', height: b.h + 'px' },
        [{ transform: 'scale(1) rotate(0)', opacity: 1 }, { transform: 'scale(.2) rotate(120deg)', opacity: 0 }], { duration: 380, easing: 'ease-in' })
        .innerHTML = `<svg viewBox="0 0 100 100"><use href="${mv.victim}"/></svg>`;
      spawn(l, 'fx-flash', { left: cx - u * .7 + 'px', top: cy - u * .7 + 'px', width: u * 1.4 + 'px', height: u * 1.4 + 'px' },
        [{ transform: 'scale(.3)', opacity: .9 }, { transform: 'scale(1.3)', opacity: 0 }], { duration: 320, easing: 'ease-out' });
    }
    if (kind === 'burst') {
      const n = big ? 26 : 14, cols = victimSide === 'w' ? ['#f4f1ea', '#d9ae6c', '#ffffff'] : ['#55514c', '#ff7a4a', '#c9c4bb'];
      for (let i = 0; i < n; i++) {
        const ang = rand(0, Math.PI * 2), dist = u * rand(.6, big ? 1.9 : 1.3), s = u * rand(.07, .15);
        spawn(l, 'fx-dot', { left: cx - s / 2 + 'px', top: cy - s / 2 + 'px', width: s + 'px', height: s + 'px', background: cols[i % cols.length] },
          [{ transform: 'translate(0,0) scale(1)', opacity: 1 }, { transform: `translate(${Math.cos(ang) * dist}px, ${Math.sin(ang) * dist}px) scale(.2)`, opacity: 0 }],
          { duration: rand(380, 620), easing: 'cubic-bezier(.1,.7,.3,1)' });
      }
    } else if (kind === 'emoji') {
      const ch = EMOJI[moverType] || '✨', n = big ? 9 : 5;
      for (let i = 0; i < n; i++) {
        const ang = rand(0, Math.PI * 2), dist = u * rand(.7, big ? 1.8 : 1.3), s = u * rand(.3, .5);
        spawn(l, 'fx-emoji', { left: cx - s / 2 + 'px', top: cy - s / 2 + 'px', width: s + 'px', height: s + 'px', fontSize: s * .9 + 'px', fontFamily: EMOJI_FONT },
          [{ transform: 'translate(0,0) scale(.4) rotate(0)', opacity: 1 }, { transform: `translate(${Math.cos(ang) * dist}px, ${Math.sin(ang) * dist - u * .3}px) scale(1) rotate(${rand(-60, 60)}deg)`, opacity: 0 }],
          { duration: rand(520, 800), easing: 'cubic-bezier(.1,.7,.3,1)' }).textContent = ch;
      }
    } else if (kind === 'shock') {
      for (let i = 0; i < (big ? 3 : 2); i++) {
        spawn(l, 'fx-ring', { left: cx - u / 2 + 'px', top: cy - u / 2 + 'px', width: u + 'px', height: u + 'px' },
          [{ transform: 'scale(.3)', opacity: .9, borderWidth: '4px' }, { transform: `scale(${big ? 3.4 : 2.4})`, opacity: 0, borderWidth: '1px' }],
          { duration: 560, delay: i * 110, easing: 'ease-out', fill: 'backwards' });
      }
    }
    if (P.shake) {
      const k = big ? 9 : 4;
      board.animate([{ transform: 'translate(0,0)' }, { transform: `translate(${-k}px, ${k / 2}px)` }, { transform: `translate(${k}px, ${-k / 2}px)` },
        { transform: `translate(${-k / 2}px, 0)` }, { transform: 'translate(0,0)' }], { duration: big ? 380 : 240, easing: 'ease-out' });
    }
  }
  function playTransition(board, mv) {
    const ms = ANIM_MS[P.anim];
    if (document.hidden) return;
    const big = typeOf(mv.victim) === 'v';
    if (ms) slide(board, mv, ms);
    if (mv.victim && !mv.restored) {
      setTimeout(() => { if (board.isConnected || board.closest('.fx-preview')) captureFx(board, mv, big); SND.capture(); }, Math.round(ms * .7));
    } else SND.move();
  }

  /* ================= Theo dõi ván đấu ================= */
  const GAME = { key: null, snap: null, board: null, baselineFinished: false, celebrated: new Set() };
  let rafPending = false;
  const gameKey = () => location.hash.split('?')[0];
  function onMainChange() {
    rafPending = false;
    const wrap = document.getElementById('board-wrap');
    const rb = document.querySelector('.round .result-box');
    const board = wrap && wrap.querySelector('.cg-board');
    injectPlay();
    if (!board) { GAME.board = null; return; }
    const key = gameKey();
    if (board !== GAME.board) {
      GAME.board = board;
      const snap = snapshot(board);
      if (snap) {
        const same = GAME.key === key && GAME.snap;
        if (same) {
          const mv = diffMove(GAME.snap, snap);
          if (mv && animOn()) playTransition(board, mv);
        } else { GAME.key = key; GAME.baselineFinished = !!rb; }
        GAME.snap = snap;
      }
    }
    if (rb && GAME.key === key && !GAME.baselineFinished && !GAME.celebrated.has(key)) {
      GAME.celebrated.add(key);
      const txt = (rb.querySelector('strong') || {}).textContent || '';
      SND.end();
      if (P.win && !document.hidden && /1–0|0–1/.test(txt)) confetti();
    }
  }
  function confetti() {
    const l = document.createElement('div'); l.className = 'fx-confetti'; document.body.appendChild(l);
    const cols = ['#ffd24a', '#ff6b6b', '#4aa3ff', '#6ee7a0', '#c58bff', '#ffffff'];
    const w = innerWidth, h = innerHeight;
    for (let i = 0; i < 90; i++) {
      const p = document.createElement('i'); p.style.left = rand(0, w) + 'px'; p.style.background = cols[i % cols.length]; l.appendChild(p);
      p.animate([{ transform: 'translate(0,0) rotate(0)', opacity: 1 }, { transform: `translate(${rand(-120, 120)}px, ${h + 40}px) rotate(${rand(300, 900)}deg)`, opacity: .9 }],
        { duration: rand(1600, 3000), delay: rand(0, 500), easing: 'cubic-bezier(.3,.1,.6,1)', fill: 'backwards' });
    }
    setTimeout(() => l.remove(), 3800);
  }

  /* ================= Phát lại tự động ================= */
  let autoT = null;
  const nav = (a) => document.querySelector(`[data-nav="${a}"]`);
  function setPlaying(on) {
    clearInterval(autoT); autoT = null;
    const b = document.getElementById('fx-play');
    if (b) { b.classList.toggle('on', on); b.textContent = on ? '⏸' : '▶'; b.setAttribute('aria-pressed', on ? 'true' : 'false'); }
    if (!on) return;
    if (nav('next') && nav('next').disabled) nav('first')?.click();
    autoT = setInterval(() => {
      const n = nav('next');
      if (!n || n.disabled) { setPlaying(false); return; }
      n.click();
    }, 900);
  }
  function injectPlay() {
    const bar = document.querySelector('.rcontrols');
    if (!bar) { if (autoT) setPlaying(false); return; }
    if (bar.querySelector('#fx-play')) return;
    const b = document.createElement('button');
    b.id = 'fx-play'; b.type = 'button'; b.textContent = '▶'; b.title = 'Tự phát lại (Space)'; b.setAttribute('aria-label', 'Tự phát lại ván đấu');
    b.addEventListener('click', () => setPlaying(!autoT));
    bar.insertBefore(b, bar.querySelector('.sep'));
  }

  /* ================= Thông báo nhỏ (dùng lại #toast) ================= */
  let sayT;
  function say(msg) {
    const t = document.getElementById('toast'); if (!t) return;
    t.textContent = msg; t.style.borderColor = ''; t.classList.add('on');
    clearTimeout(sayT); sayT = setTimeout(() => t.classList.remove('on'), 1800);
  }

  /* ================= Bảng cài đặt ================= */
  const ICO_PALETTE = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3a9 9 0 1 0 0 18c1.1 0 1.8-.8 1.8-1.7 0-.5-.2-.8-.5-1.2-.3-.4-.5-.7-.5-1.2 0-1 .8-1.7 1.8-1.7H17a4 4 0 0 0 4-4C21 6.6 17 3 12 3z"/><circle cx="7.5" cy="11" r="1"/><circle cx="10" cy="7.5" r="1"/><circle cx="14.5" cy="7.5" r="1"/></svg>';
  const SAMPLE = { // bàn minh họa (không xoay): ô -> [bên, loại]
    before: { c5: ['w', 'k'], c6: ['b', 'a'], d3: ['w', 'b'], e7: ['b', 'k'], f6: ['b', 'b'], f2: ['w', 'a'], e1: ['w', 'v'], e9: ['b', 'v'], g4: ['w', 'k'] },
    move: ['c5', 'c6'],
  };
  function sampleBoard(moved) {
    const pos = { ...SAMPLE.before };
    let last = null;
    if (moved) { const [f, t] = SAMPLE.move; pos[t] = pos[f]; delete pos[f]; last = [f, t]; }
    let h = '<div class="cg-board">';
    for (let r = 8; r >= 0; r--) for (let f = 0; f < 9; f++) {
      const s = FILES[f] + (r + 1), pc = pos[s];
      let c = 'sq ' + ((f + r) % 2 === 0 ? 'dark' : 'light'); if (last && last.includes(s)) c += ' last';
      let inner = '';
      if (f === 8) inner += `<span class="coord rank">${r + 1}</span>`;
      if (r === 0) inner += `<span class="coord file">${FILES[f]}</span>`;
      if (pc) inner += `<svg class="piece" viewBox="0 0 100 100"><use href="#pc-${pc[0]}${pc[1]}"/></svg>`;
      h += `<div class="${c}">${inner}</div>`;
    }
    return h + '</div>';
  }
  function tmpSnap(html) { const t = document.createElement('div'); t.innerHTML = html; return snapshot(t.firstChild); }

  let panel = null, backdrop = null, opener = null;
  const chipRow = (key, label) => `<div class="fx-sec"><h3>${label}</h3><div class="fx-chips" role="radiogroup" aria-label="${label}" data-key="${key}">${
    OPT[key].map(([id, n]) => `<button type="button" class="fx-chip" role="radio" data-v="${id}">${n}</button>`).join('')}</div></div>`;
  const toggle = (key, label) => `<div class="fx-row"><label for="fx-t-${key}">${label}</label><input type="checkbox" id="fx-t-${key}" data-tg="${key}"></div>`;
  function buildPanel() {
    backdrop = document.createElement('div'); backdrop.className = 'fx-backdrop'; backdrop.hidden = true;
    panel = document.createElement('aside'); panel.className = 'fx-panel'; panel.hidden = true;
    panel.setAttribute('role', 'dialog'); panel.setAttribute('aria-modal', 'true'); panel.setAttribute('aria-label', 'Giao diện và hiệu ứng');
    const pieceCards = OPT.pieces.map(([id, n]) => `<button type="button" class="fx-card" role="radio" data-v="${id}"><span class="pcs">${
      ['k', 'b', 'a', 'v'].map((t) => `<svg viewBox="0 0 100 100">${pieceMarkup(id, id === 'neon' ? 'b' : 'w', t)}</svg>`).join('')}</span>${n}</button>`).join('');
    const boardCards = OPT.board.map(([id, n]) => `<button type="button" class="fx-card" role="radio" data-v="${id}"><span class="cg-board" data-board="${id}">${
      Array.from({ length: 81 }, (_, i) => `<span class="sq ${(Math.floor(i / 9) + (i % 9)) % 2 ? 'light' : 'dark'}"></span>`).join('')}</span>${n}</button>`).join('');
    const uiCards = OPT.ui.map(([id, n]) => `<button type="button" class="fx-card" role="radio" data-v="${id}"><span class="fx-ui-sw" data-ui="${id}"><i></i><i></i><i></i></span>${n}</button>`).join('');
    const dots = OPT.accent.map(([id, n]) => `<button type="button" class="fx-dot-btn" role="radio" data-v="${id}" data-accent="${id}" title="${n}" aria-label="${n}"></button>`).join('');
    panel.innerHTML = `<div class="fx-head"><h2>Giao diện &amp; hiệu ứng</h2><button type="button" class="fx-x" data-close aria-label="Đóng">×</button></div>
      <div class="fx-body">
        <div class="fx-preview" id="fx-pv"></div>
        <div class="fx-actions" style="margin-top:0;justify-content:center"><button type="button" class="button small" data-demo>▶ Thử hiệu ứng</button></div>
        <div class="fx-sec"><h3>Nền giao diện</h3><div class="fx-grid" role="radiogroup" aria-label="Nền giao diện" data-key="ui">${uiCards}</div></div>
        <div class="fx-sec"><h3>Màu nhấn</h3><div class="fx-dots" role="radiogroup" aria-label="Màu nhấn" data-key="accent">${dots}</div></div>
        <div class="fx-sec"><h3>Bàn cờ</h3><div class="fx-grid" role="radiogroup" aria-label="Theme bàn cờ" data-key="board">${boardCards}</div></div>
        <div class="fx-sec"><h3>Bộ quân</h3><div class="fx-grid" role="radiogroup" aria-label="Bộ quân" data-key="pieces">${pieceCards}</div></div>
        ${chipRow('pscale', 'Cỡ quân')}${chipRow('last', 'Nước đi cuối')}${chipRow('radius', 'Góc bàn cờ')}
        <div class="fx-sec"><h3>Hiển thị</h3>${toggle('coords', 'Hiện tọa độ a–i / 1–9')}</div>
        ${chipRow('anim', 'Tốc độ di chuyển quân')}${chipRow('capfx', 'Hiệu ứng ăn quân')}
        <div class="fx-sec"><h3>Hiệu ứng khác</h3>${toggle('shake', 'Rung bàn cờ khi ăn quân')}${toggle('win', 'Pháo giấy khi ván kết thúc')}</div>
        <div class="fx-sec"><h3>Âm thanh</h3>${toggle('sound', 'Bật âm thanh nước đi')}<div class="fx-row"><label for="fx-vol">Âm lượng</label><input type="range" id="fx-vol" min="0" max="100" step="5"></div></div>
        <div class="fx-keys"><kbd>←</kbd> <kbd>→</kbd> duyệt nước · <kbd>↑</kbd> <kbd>↓</kbd> đầu/cuối · <kbd>F</kbd> xoay bàn · <kbd>Space</kbd> tự phát · <kbd>B</kbd> đổi theme bàn cờ</div>
        <div class="fx-actions"><button type="button" class="button outline small" data-reset>Đặt lại mặc định</button></div>
      </div>`;
    document.body.append(backdrop, panel);
    panel.addEventListener('click', (e) => {
      if (e.target.closest('[data-close]')) return closePanel();
      if (e.target.closest('[data-reset]')) { Object.assign(P, DEFAULTS); commit(true); return; }
      if (e.target.closest('[data-demo]')) return demo();
      const b = e.target.closest('[data-v]'); if (!b) return;
      const g = b.closest('[data-key]'); if (!g) return;
      P[g.dataset.key] = b.dataset.v; commit(g.dataset.key === 'pieces');
      if (g.dataset.key === 'anim' || g.dataset.key === 'capfx') demo();
    });
    panel.addEventListener('change', (e) => {
      const t = e.target;
      if (t.dataset.tg) { P[t.dataset.tg] = t.checked; commit(false); if (t.dataset.tg === 'sound' && t.checked) SND.tick(); }
      else if (t.id === 'fx-vol') { P.vol = +t.value; save(); SND.tick(); }
    });
    backdrop.addEventListener('click', closePanel);
    refreshPanel();
  }
  function commit(pieces) { applyAttrs(); if (pieces) applyPieces(); save(); refreshPanel(); }
  function refreshPanel() {
    if (!panel) return;
    panel.querySelectorAll('[data-key]').forEach((g) => g.querySelectorAll('[data-v]').forEach((b) => b.setAttribute('aria-checked', String(b.dataset.v === String(P[g.dataset.key])))));
    panel.querySelectorAll('[data-tg]').forEach((c) => { c.checked = !!P[c.dataset.tg]; });
    const v = panel.querySelector('#fx-vol'); if (v) v.value = P.vol;
  }
  function renderPreview(moved) { const pv = document.getElementById('fx-pv'); if (pv) pv.innerHTML = sampleBoard(moved); }
  let demoT;
  function demo() {
    const pv = document.getElementById('fx-pv'); if (!pv) return;
    clearTimeout(demoT); renderPreview(false);
    const before = tmpSnap(sampleBoard(false));
    demoT = setTimeout(() => {
      renderPreview(true);
      const board = pv.firstChild, mv = diffMove(before, snapshot(board));
      if (mv) {
        if (ANIM_MS[P.anim]) playTransition(board, mv);
        else { SND.capture(); setTimeout(() => captureFx(board, mv, false), 0); }
      }
    }, 250);
  }
  function openPanel() {
    if (!panel) buildPanel();
    opener = document.activeElement; renderPreview(false);
    backdrop.hidden = panel.hidden = false;
    requestAnimationFrame(() => { backdrop.classList.add('on'); panel.classList.add('on'); panel.querySelector('[data-close]').focus(); });
  }
  function closePanel() {
    if (!panel || panel.hidden) return;
    backdrop.classList.remove('on'); panel.classList.remove('on');
    setTimeout(() => { backdrop.hidden = panel.hidden = true; }, 240);
    if (opener && opener.focus) opener.focus();
  }
  const isOpen = () => panel && !panel.hidden && panel.classList.contains('on');

  /* ================= Header, điều hướng mobile, phím tắt ================= */
  function injectHeaderBtn() {
    const tr = document.querySelector('#top .top-right');
    if (!tr || tr.querySelector('#fx-open')) return;
    const b = document.createElement('button');
    b.id = 'fx-open'; b.type = 'button'; b.className = 'top-icon'; b.title = 'Giao diện & hiệu ứng'; b.setAttribute('aria-label', 'Giao diện và hiệu ứng'); b.innerHTML = ICO_PALETTE;
    b.addEventListener('click', openPanel);
    tr.insertBefore(b, tr.firstChild);
  }
  const NAV_ICO = {
    home: '<path d="M3 11l9-8 9 8M5 10v10h14V10"/>', trophy: '<path d="M8 4h8v5a4 4 0 0 1-8 0V4zM8 6H4v1a3 3 0 0 0 3 3M16 6h4v1a3 3 0 0 1-3 3M12 13v4M8 21h8M10 17h4"/>',
    eye: '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>', code: '<path d="M8 8l-5 4 5 4M16 8l5 4-5 4M14 5l-4 14"/>',
    user: '<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-6 8-6s8 2 8 6"/>', shield: '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6l8-3z"/>',
  };
  const iconFor = (href) => /^#\/tournaments?/.test(href) ? 'trophy' : /^#\/(watch|game)/.test(href) ? 'eye' : /^#\/(bot|code)/.test(href) ? 'code'
    : /^#\/user/.test(href) ? 'user' : /^#\/organizer/.test(href) ? 'shield' : 'home';
  function syncMnav() {
    let nav = document.getElementById('mnav');
    if (!nav) { nav = document.createElement('nav'); nav.id = 'mnav'; nav.setAttribute('aria-label', 'Điều hướng nhanh'); document.body.appendChild(nav); }
    const items = [...document.querySelectorAll('#top .site-nav .nav-item')].map((n) => {
      const a = n.querySelector(':scope > a'); return a ? { href: a.getAttribute('href'), label: a.textContent.trim(), on: n.classList.contains('active') } : null;
    }).filter((i, n, all) => i && all.findIndex((x) => x && x.href === i.href) === n);
    if (!document.querySelector('#top .user-link')) items.push({ href: '#/login', label: 'Đăng nhập', on: /^#\/(login|register)/.test(location.hash) });
    const sig = items.map((i) => i.href + i.label + i.on).join('|');
    if (nav.dataset.sig === sig) return;
    nav.dataset.sig = sig;
    nav.innerHTML = items.map((i) => `<a href="${i.href}" class="${i.on ? 'on' : ''}"${i.on ? ' aria-current="page"' : ''}><svg viewBox="0 0 24 24" aria-hidden="true">${NAV_ICO[iconFor(i.href)]}</svg>${i.label}</a>`).join('')
      + `<button type="button" data-fx-open><svg viewBox="0 0 24 24" aria-hidden="true">${ICO_PALETTE.replace(/^<svg[^>]*>|<\/svg>$/g, '')}</svg>Giao diện</button>`;
  }
  document.addEventListener('click', (e) => { if (e.target.closest('[data-fx-open]')) openPanel(); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && isOpen()) { closePanel(); return; }
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.target.matches && e.target.matches('input, textarea, select, [contenteditable]')) return;
    if (!document.querySelector('.round')) return;
    if (e.key === ' ' && (e.target === document.body || e.target === document.documentElement)) { e.preventDefault(); setPlaying(!autoT); }
    else if (e.key === 'Home') { nav('first')?.click(); e.preventDefault(); }
    else if (e.key === 'End') { nav('last')?.click(); e.preventDefault(); }
    else if (e.key === 'b' || e.key === 'B') {
      const ids = OPT.board.map(([id]) => id), i = ids.indexOf(P.board);
      P.board = ids[(i + (e.shiftKey ? ids.length - 1 : 1)) % ids.length]; commit(false);
      say('Bàn cờ: ' + OPT.board.find(([id]) => id === P.board)[1]);
    }
  });
  window.addEventListener('pointerdown', unlock, { once: true });
  window.addEventListener('hashchange', () => {
    setPlaying(false);
    const m = document.getElementById('main'); if (!m) return;
    m.classList.add('route-anim'); setTimeout(() => m.classList.remove('route-anim'), 400);
  });

  function boot() {
    initPieces();
    const top = document.getElementById('top'), main = document.getElementById('main');
    const hdr = () => { injectHeaderBtn(); syncMnav(); };
    if (top) { new MutationObserver(hdr).observe(top, { childList: true }); hdr(); }
    if (main) {
      new MutationObserver(() => { if (!rafPending) { rafPending = true; setTimeout(onMainChange, 16); } }).observe(main, { childList: true, subtree: true });
      onMainChange();
    }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
  window.OTT_THEMES = { prefs: P, open: openPanel, snapshot, diffMove };
})();
