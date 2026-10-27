// BFF demo (C04): Cloudflare Worker phục vụ static assets và proxy /api/v1/* tới origin (Quick Tunnel).
// Hợp đồng: docs/minh/bff-contract.md. Mọi giá trị cấu hình lấy từ env, không nhận đích từ client.

export const MAX_BODY = 1024 * 1024;      // 1 MiB, khớp thiết kế mục 7.2/15.3
const ORIGIN_TIMEOUT_MS = 25_000;
const SEG = '[A-Za-z0-9_-]+';
const TEXT_RESPONSE = new RegExp(`^/(bot-template|bots/${SEG}/source)$`);

// Allowlist method + path (phần sau /api/v1). Khớp bảng route của ott/api.py.
const ROUTES = [
  ['POST', new RegExp('^/auth/(register|login|logout|verify-email)$')],
  ['GET', new RegExp('^/(me|stats|tournaments|live|bot-template|ops/status)$')],
  ['POST', new RegExp('^/ops/maintenance$')],
  ['POST', new RegExp('^/tournaments$')],
  ['GET', new RegExp(`^/tournaments/${SEG}$`)],
  ['PATCH', new RegExp(`^/tournaments/${SEG}$`)],
  ['GET', new RegExp(`^/tournaments/${SEG}/(schedule|standings|entries|entry|code)$`)],
  ['POST', new RegExp(`^/tournaments/${SEG}/(entry|schedule|resume|code/check|code/submit|open-registration|start-coding|lock|start|cancel)$`)],
  ['PUT', new RegExp(`^/tournaments/${SEG}/code$`)],
  ['DELETE', new RegExp(`^/entries/${SEG}$`)],
  ['GET', new RegExp(`^/(bots/${SEG}/source|matches/${SEG}|users/${SEG})$`)],
];
const PATH_EXISTS = (sub) => ROUTES.some(([, re]) => re.test(sub));
const METHOD_OK = (m, sub) => ROUTES.some(([rm, re]) => rm === m && re.test(sub));

// Header client được phép chuyển tiếp. Mọi header khác (x-forwarded-*, x-contest-origin-key, cf-*, host...) bị bỏ.
const FORWARD_HEADERS = ['accept', 'accept-language', 'content-type', 'cookie', 'origin', 'x-csrf-token', 'user-agent', 'sec-fetch-site'];
const encoder = new TextEncoder();

function base64(value) {
  const bytes = typeof value === 'string' ? encoder.encode(value) : value;
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

async function sendVerificationEmail(env, recipient, verificationUrl) {
  const sender = env.GMAIL_ADDRESS || '';
  const password = (env.GMAIL_APP_PASSWORD || '').replace(/\s/g, '');
  if (!/^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$/.test(sender)
      || !/^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$/.test(recipient)
      || !password) throw new Error('Mail configuration is missing.');

  const { connect } = await import('cloudflare:sockets');
  const socket = connect({ hostname: 'smtp.gmail.com', port: 465 }, { secureTransport: 'on' });
  const reader = socket.readable.getReader();
  const writer = socket.writable.getWriter();
  let buffered = '';

  async function readReply() {
    const lines = [];
    for (;;) {
      const end = buffered.indexOf('\r\n');
      if (end < 0) {
        let timer;
        let chunk;
        try {
          chunk = await Promise.race([
            reader.read(),
            new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('SMTP timeout.')), 10000); }),
          ]);
        } finally { clearTimeout(timer); }
        if (chunk.done) throw new Error('SMTP connection closed.');
        buffered += new TextDecoder().decode(chunk.value, { stream: true });
        if (buffered.length > 16384) throw new Error('SMTP response too large.');
        continue;
      }
      const line = buffered.slice(0, end);
      buffered = buffered.slice(end + 2);
      if (!/^\d{3}[ -]/.test(line)) throw new Error('Invalid SMTP response.');
      lines.push(line);
      if (line[3] === ' ') return { code: Number(line.slice(0, 3)), lines };
    }
  }

  async function command(value, expected) {
    await writer.write(encoder.encode(value + '\r\n'));
    const result = await readReply();
    if (!expected.includes(result.code)) throw new Error('SMTP command failed.');
    return result;
  }

  try {
    await socket.opened;
    if ((await readReply()).code !== 220) throw new Error('SMTP greeting failed.');
    await command('EHLO ott-arena-demo.workers.dev', [250]);
    await command(`AUTH PLAIN ${base64(`\0${sender}\0${password}`)}`, [235]);
    await command(`MAIL FROM:<${sender}>`, [250]);
    await command(`RCPT TO:<${recipient}>`, [250, 251]);
    await command('DATA', [354]);
    const subject = base64('Xác minh tài khoản OTT Arena');
    const body = base64(
      `Bạn vừa đăng ký tài khoản OTT Arena. Mở liên kết sau để xác minh email:\n\n${verificationUrl}\n\n` +
      'Liên kết hết hạn sau 24 giờ. Nếu bạn không đăng ký, hãy bỏ qua thư này.'
    ).match(/.{1,76}/g).join('\r\n');
    const message = [
      `From: OTT Arena <${sender}>`, `To: ${recipient}`,
      `Subject: =?UTF-8?B?${subject}?=`, `Date: ${new Date().toUTCString()}`,
      `Message-ID: <${crypto.randomUUID()}@ott-arena-demo.workers.dev>`,
      'MIME-Version: 1.0', 'Content-Type: text/plain; charset=UTF-8',
      'Content-Transfer-Encoding: base64', '', body,
    ].join('\r\n') + '\r\n.\r\n';
    await writer.write(encoder.encode(message));
    await readReply().then((reply) => { if (reply.code !== 250) throw new Error('SMTP delivery failed.'); });
    await command('QUIT', [221]);
  } finally {
    await reader.cancel().catch(() => {});
    writer.releaseLock();
    await socket.close().catch(() => {});
  }
}

function json(status, code, message, requestId, extra = {}) {
  return new Response(JSON.stringify({ error: { code, message, request_id: requestId } }), {
    status,
    headers: { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store', 'X-Request-ID': requestId, 'X-Content-Type-Options': 'nosniff', ...extra },
  });
}

function runtimeConfig(env) {
  const mode = env.RUNTIME_MODE === 'local' ? 'local' : 'tunnel';
  const pollMs = Math.max(10_000, Number(env.POLL_MS) || 10_000);
  return new Response(`window.OTT_CONFIG = ${JSON.stringify({ mode, pollMs })};\n`, {
    headers: { 'Content-Type': 'application/javascript; charset=utf-8', 'Cache-Control': 'no-store' },
  });
}

// Đọc body theo từng chunk và dừng ngay khi vượt trần, kể cả khi thiếu Content-Length.
async function readBodyLimited(request, limit) {
  const declared = request.headers.get('content-length');
  if (declared !== null && (!/^\d+$/.test(declared) || Number(declared) > limit)) return { tooLarge: true };
  if (!request.body) return { bytes: null };
  const reader = request.body.getReader();
  const initialSize = declared === null ? 4 * 1024 : Math.min(Math.max(Number(declared), 1024), 16 * 1024);
  let bytes = new Uint8Array(Math.min(limit, initialSize));
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    const nextTotal = total + value.byteLength;
    if (nextTotal > limit) { await reader.cancel().catch(() => {}); return { tooLarge: true }; }
    if (nextTotal > bytes.length) {
      const capacity = Math.min(limit, Math.max(nextTotal, bytes.length * 2, 16 * 1024));
      const expanded = new Uint8Array(capacity);
      expanded.set(bytes.subarray(0, total));
      bytes = expanded;
    }
    bytes.set(value, total);
    total = nextTotal;
  }
  return { bytes: bytes.subarray(0, total) };
}

function stripCookieDomain(value) {
  return value.split(';').filter((p) => !/^\s*domain\s*=/i.test(p)).join(';');
}

function publicOrigin(env, url) {
  return (env.PUBLIC_ORIGIN || '').replace(/\/+$/, '') || url.origin;
}

async function handleApi(request, env, url, requestId) {
  const method = request.method;
  if (!url.pathname.startsWith('/api/v1/')) return json(404, 'NOT_FOUND', 'No such endpoint.', requestId);
  const sub = url.pathname.slice('/api/v1'.length);

  if (sub === '/stream') return json(404, 'STREAM_DISABLED', 'Live stream is disabled in demo mode; use polling.', requestId);
  if (!PATH_EXISTS(sub)) return json(404, 'NOT_FOUND', 'No such endpoint.', requestId);
  if (!METHOD_OK(method, sub)) return json(405, 'METHOD_NOT_ALLOWED', 'Method not allowed.', requestId, { Allow: ROUTES.filter(([, re]) => re.test(sub)).map(([m]) => m).join(', ') });

  let origin;
  try { origin = new URL(env.ORIGIN_URL); } catch { origin = null; }
  const localHttpOrigin = origin?.protocol === 'http:'
    && ['localhost', '127.0.0.1', '[::1]'].includes(origin.hostname);
  const quickTunnelOrigin = origin?.protocol === 'https:'
    && /^[a-z0-9-]+\.trycloudflare\.com$/.test(origin.hostname);
  if (!origin || !env.ORIGIN_KEY || (!quickTunnelOrigin && !localHttpOrigin)
      || origin.username || origin.password || origin.pathname !== '/' || origin.search || origin.hash) {
    return json(503, 'ORIGIN_NOT_CONFIGURED', 'Origin is not configured.', requestId, { 'Retry-After': '30' });
  }

  const mutating = method !== 'GET';
  if (mutating) {
    if (request.headers.get('sec-fetch-site') === 'cross-site') return json(403, 'FORBIDDEN', 'Cross-site mutation rejected.', requestId);
    // CSRF lớp 1: Origin bắt buộc và phải đúng origin công khai (kể cả login/register).
    if (request.headers.get('origin') !== publicOrigin(env, url)) return json(403, 'FORBIDDEN', 'Cross-origin request rejected.', requestId);
  }

  let body;
  if (mutating) {
    const ct = (request.headers.get('content-type') || '').toLowerCase();
    const limited = await readBodyLimited(request, MAX_BODY);
    if (limited.tooLarge) return json(413, 'PAYLOAD_TOO_LARGE', 'Request body is too large.', requestId);
    if (limited.bytes && limited.bytes.byteLength > 0) {
      if (!ct.startsWith('application/json')) return json(415, 'UNSUPPORTED_MEDIA_TYPE', 'Only application/json is accepted.', requestId);
      body = limited.bytes;
    }
  }

  const headers = new Headers();
  for (const name of FORWARD_HEADERS) { const v = request.headers.get(name); if (v !== null) headers.set(name, v); }
  const clientIp = request.headers.get('CF-Connecting-IP');
  if (clientIp) headers.set('X-Contest-Client-IP', clientIp);
  headers.set('X-Contest-Origin-Key', env.ORIGIN_KEY);   // luôn ghi đè, không bao giờ lấy từ client
  headers.set('X-Request-ID', requestId);

  const target = new URL(origin.origin + url.pathname + url.search);
  let res;
  try {
    // redirect: manual (không đi theo, không gửi cookie/key sang host khác); không retry tự động, nhất là POST.
    res = await fetch(target.toString(), { method, headers, body, redirect: 'manual', signal: AbortSignal.timeout(ORIGIN_TIMEOUT_MS) });
  } catch (e) {
    if (e && (e.name === 'TimeoutError' || e.name === 'AbortError')) return json(504, 'ORIGIN_TIMEOUT', 'Origin timed out.', requestId, { 'Retry-After': '10' });
    return json(503, 'ORIGIN_UNAVAILABLE', 'Origin is unavailable.', requestId, { 'Retry-After': '10' });
  }

  if (res.status >= 300 && res.status < 400) {
    await res.body?.cancel().catch(() => {});
    return json(502, 'ORIGIN_REDIRECT', 'Unexpected redirect from origin.', requestId);
  }
  const ctype = res.headers.get('content-type') || '';
  const mediaType = ctype.split(';')[0].trim().toLowerCase();
  const isJson = mediaType === 'application/json' || (mediaType.startsWith('application/') && mediaType.endsWith('+json'));
  const isText = method === 'GET' && res.ok && TEXT_RESPONSE.test(sub) && mediaType === 'text/plain';
  if (res.status !== 204 && !isJson && !isText) {
    await res.body?.cancel().catch(() => {});
    // Trang lỗi HTML của tunnel/edge hoặc index.html: không chuyển cho client.
    const down = res.status >= 500;
    return json(down ? 503 : 502, down ? 'ORIGIN_UNAVAILABLE' : 'BAD_ORIGIN_RESPONSE', 'Origin returned an unexpected response.', requestId, down ? { 'Retry-After': '10' } : {});
  }

  if (sub === '/auth/register' && res.status === 201) {
    let registration;
    try { registration = await res.json(); } catch { registration = null; }
    if (!registration?.verification_required
        || !/^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$/.test(registration.email || '')
        || !/^[A-Za-z0-9_-]{32,128}$/.test(registration.token || '')) {
      return json(502, 'BAD_ORIGIN_RESPONSE', 'Origin returned an unexpected registration response.', requestId);
    }
    try {
      const verifyUrl = `${publicOrigin(env, url)}/#/verify/${registration.token}`;
      await sendVerificationEmail(env, registration.email, verifyUrl);
    } catch {
      return json(503, 'EMAIL_UNAVAILABLE', 'Could not send verification email. Retry signup shortly.', requestId, { 'Retry-After': '30' });
    }
    return new Response(JSON.stringify({ verification_required: true }), {
      status: 202,
      headers: {
        'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store',
        'X-Request-ID': requestId, 'X-Content-Type-Options': 'nosniff',
      },
    });
  }

  const out = new Headers();
  if (ctype) out.set('Content-Type', ctype);
  const ra = res.headers.get('retry-after'); if (ra) out.set('Retry-After', ra);
  const cookies = typeof res.headers.getSetCookie === 'function' ? res.headers.getSetCookie() : [];
  for (const c of cookies) out.append('Set-Cookie', stripCookieDomain(c)); // cookie host-only
  out.set('Cache-Control', 'no-store');
  out.set('X-Request-ID', requestId);
  out.set('X-Content-Type-Options', 'nosniff');
  return new Response(res.status === 204 ? null : res.body, { status: res.status, headers: out });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const requestId = crypto.randomUUID();
    if (url.pathname === '/runtime-config.js') return runtimeConfig(env);
    if (url.pathname === '/api' || url.pathname.startsWith('/api/')) return handleApi(request, env, url, requestId);
    return env.ASSETS.fetch(request);
  },
};
