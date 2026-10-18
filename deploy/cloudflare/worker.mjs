// Private origin proxy. Submission code never runs in this Worker.
const BODY_LIMIT = 1024 * 1024;
const ID = "[0-9a-f-]{36}";
const ROUTES = [
  ["POST", /^\/api\/v1\/auth\/(login|logout|register)$/],
  ["GET", /^\/api\/v1\/(me|stats|live|bot-template|tournaments|ops\/status)$/],
  ["POST", /^\/api\/v1\/(tournaments|ops\/maintenance)$/],
  ["GET", new RegExp(`^/api/v1/tournaments/${ID}(/(standings|schedule|entries|entry|code))?$`)],
  ["PATCH", new RegExp(`^/api/v1/tournaments/${ID}$`)],
  ["POST", new RegExp(`^/api/v1/tournaments/${ID}/(open-registration|start-coding|lock|start|cancel|schedule|resume|entry|code/check|code/submit)$`)],
  ["PUT", new RegExp(`^/api/v1/tournaments/${ID}/code$`)],
  ["DELETE", new RegExp(`^/api/v1/entries/${ID}$`)],
  ["GET", new RegExp(`^/api/v1/(matches/${ID}|bots/${ID}/source|users/[A-Za-z0-9_]{3,32})$`)],
];

function failure(status, code, message) {
  return Response.json({ error: { code, message } }, {
    status,
    headers: { "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
      ...([429, 503, 504].includes(status) ? { "Retry-After": "5" } : {}) },
  });
}

async function readBody(request) {
  const declared = request.headers.get("Content-Length");
  if (declared !== null && (!/^\d+$/.test(declared) || Number(declared) > BODY_LIMIT)) {
    throw new RangeError("Request body exceeds 1 MiB.");
  }
  if (!request.body) return null;
  const reader = request.body.getReader();
  const chunks = [];
  let length = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      length += value.byteLength;
      if (length > BODY_LIMIT) {
        await reader.cancel();
        throw new RangeError("Request body exceeds 1 MiB.");
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const body = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) { body.set(chunk, offset); offset += chunk.byteLength; }
  return body;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (!url.pathname.startsWith("/api/")) {
      const asset = await env.ASSETS.fetch(request);
      const safe = new Response(asset.body, asset);
      safe.headers.set("X-Content-Type-Options", "nosniff");
      safe.headers.set("Referrer-Policy", "same-origin");
      safe.headers.set("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'");
      return safe;
    }
    if (!ROUTES.some(([method, pattern]) => method === request.method && pattern.test(url.pathname))) {
      return failure(404, "NOT_FOUND", "API route is unavailable.");
    }
    if (url.href.length > 8192) return failure(414, "URI_TOO_LONG", "Request target is too long.");
    let origin;
    try {
      origin = new URL(env.ORIGIN_URL);
      if (origin.protocol !== "https:" || origin.username || origin.password || origin.port
          || origin.pathname !== "/" || origin.search || origin.hash
          || !/^[a-z0-9-]+\.trycloudflare\.com$/.test(origin.hostname)
          || url.origin !== env.PUBLIC_ORIGIN || typeof env.ORIGIN_KEY !== "string"
          || env.ORIGIN_KEY.length < 32 || !/^[\x20-\x7e]+$/.test(env.ORIGIN_KEY)) {
        throw new Error("Invalid origin configuration.");
      }
    } catch {
      return failure(503, "GATEWAY_NOT_CONFIGURED", "Gateway configuration is incomplete.");
    }
    const mutation = !["GET", "HEAD"].includes(request.method);
    if (mutation && request.headers.get("Origin") !== env.PUBLIC_ORIGIN) {
      return failure(403, "FORBIDDEN", "Public origin does not match.");
    }
    if (request.headers.get("Content-Encoding") && request.headers.get("Content-Encoding") !== "identity") {
      return failure(415, "UNSUPPORTED_ENCODING", "Encoded request bodies are unsupported.");
    }
    if (mutation && request.body && request.headers.get("Content-Type")?.split(";")[0].trim() !== "application/json") {
      return failure(415, "UNSUPPORTED_MEDIA_TYPE", "Content-Type must be application/json.");
    }
    let body;
    try {
      body = mutation ? await readBody(request) : null;
    } catch (error) {
      return failure(error instanceof RangeError ? 413 : 400, "INVALID_BODY", "Request body is invalid or too large.");
    }
    // Construct a fresh header set. Client-supplied proxy identity is discarded.
    const headers = new Headers();
    for (const name of ["Cookie", "X-CSRF-Token", "Content-Type"]) {
      const value = request.headers.get(name);
      if (value !== null) headers.set(name, value);
    }
    headers.set("X-Contest-Origin-Key", env.ORIGIN_KEY);
    headers.set("X-Contest-Client-IP", request.headers.get("CF-Connecting-IP") || "0.0.0.0");
    if (mutation) headers.set("Origin", env.PUBLIC_ORIGIN);
    const upstream = new URL(url.pathname + url.search, origin);
    try {
      const response = await fetch(upstream, { method: request.method, headers, body,
        redirect: "manual", signal: AbortSignal.timeout(90_000),
        cf: { cacheTtl: 0, cacheEverything: false } });
      if (response.status >= 300 && response.status < 400) {
        await response.body?.cancel();
        return failure(502, "ORIGIN_REDIRECT", "Origin returned an unexpected redirect.");
      }
      const outgoing = new Headers(response.headers);
      for (const name of ["Connection", "Transfer-Encoding", "Keep-Alive", "Server", "Location",
        "X-Contest-Origin-Key", "Access-Control-Allow-Origin", "Access-Control-Allow-Credentials"]) outgoing.delete(name);
      outgoing.set("Cache-Control", "no-store");
      outgoing.set("X-Content-Type-Options", "nosniff");
      return new Response(response.body, { status: response.status, headers: outgoing });
    } catch (error) {
      return failure(error?.name === "TimeoutError" ? 504 : 503, "ORIGIN_UNAVAILABLE", "Origin is unavailable; retry later.");
    }
  },
};
