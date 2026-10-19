// BFF demo (C04): Cloudflare Worker phục vụ static assets và proxy /api/v1/* tới origin (Quick Tunnel).
// Hợp đồng: docs/minh/bff-contract.md. Mọi giá trị cấu hình lấy từ env, không nhận đích từ client.

export const MAX_BODY = 1024 * 1024;      // 1 MiB, khớp thiết kế mục 7.2/15.3
const ORIGIN_TIMEOUT_MS = 25_000;
const SEG = '[A-Za-z0-9_-]+';

// Allowlist method + path (phần sau /api/v1). Khớp bảng route của ott/api.py.
const ROUTES = [
  ['POST', new RegExp('^/auth/(register|login|logout)$')],
  ['GET', new RegExp('^/(me|stats|tournaments|live|bot-template)$')],
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
const FORWARD_HEADERS = ['accept', 'accept-language', 'content-type', 'cookie', 'origin', 'x-csrf-token', 'user-agent'];

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
  const chunks = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > limit) { await reader.cancel().catch(() => {}); return { tooLarge: true }; }
    chunks.push(value);
  }
  const bytes = new Uint8Array(total);
  let off = 0;
  for (const c of chunks) { bytes.set(c, off); off += c.byteLength; }
  return { bytes };
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
  if (!origin || !env.ORIGIN_KEY || !/^https?:$/.test(origin.protocol)) return json(503, 'ORIGIN_NOT_CONFIGURED', 'Origin is not configured.', requestId, { 'Retry-After': '30' });

  const mutating = method !== 'GET';
  if (mutating) {
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

  if (res.status >= 300 && res.status < 400) return json(502, 'ORIGIN_REDIRECT', 'Unexpected redirect from origin.', requestId);
  const ctype = res.headers.get('content-type') || '';
  const isJson = ctype.toLowerCase().includes('json');
  if (res.status !== 204 && !isJson) {
    // Trang lỗi HTML của tunnel/edge hoặc index.html: không chuyển cho client.
    const down = res.status >= 500;
    return json(down ? 503 : 502, down ? 'ORIGIN_UNAVAILABLE' : 'BAD_ORIGIN_RESPONSE', 'Origin returned an unexpected response.', requestId, down ? { 'Retry-After': '10' } : {});
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
