#!/usr/bin/env node
// Giả lập Cloudflare (assets + Worker) trên máy để diễn tập/kiểm thử không cần deploy.
// Dùng: ORIGIN_URL=http://127.0.0.1:8080 ORIGIN_KEY=dev-key node dev-server.mjs   (cổng mặc định 8787)
import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import worker from './src/worker.js';

const PORT = Number(process.env.PORT || 8787);
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../mockup');
const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'application/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.png': 'image/png' };
const IGNORED = /^\/(README\.md|screenshots\/|\.assetsignore)/;
const env = {
  ORIGIN_URL: process.env.ORIGIN_URL || 'http://127.0.0.1:8080', ORIGIN_KEY: process.env.ORIGIN_KEY || 'dev-key',
  PUBLIC_ORIGIN: process.env.PUBLIC_ORIGIN || `http://127.0.0.1:${PORT}`, RUNTIME_MODE: process.env.RUNTIME_MODE || 'tunnel', POLL_MS: process.env.POLL_MS || '10000',
  ASSETS: { async fetch(req) {
    let p = new URL(req.url).pathname; if (p === '/') p = '/index.html';
    const file = path.resolve(ROOT, '.' + p);
    if (IGNORED.test(p) || !file.startsWith(ROOT + path.sep)) return new Response('Not found', { status: 404 });
    try { return new Response(await readFile(file), { headers: { 'Content-Type': TYPES[path.extname(file)] || 'application/octet-stream' } }); }
    catch { return new Response('Not found', { status: 404 }); }
  } },
};
http.createServer(async (req, res) => {
  const chunks = []; for await (const c of req) chunks.push(c);
  const body = chunks.length ? Buffer.concat(chunks) : undefined;
  const r = await worker.fetch(new Request(`http://${req.headers.host}${req.url}`, { method: req.method, headers: req.headers, body: ['GET', 'HEAD'].includes(req.method) ? undefined : body }), env);
  const h = {}; for (const [k, v] of r.headers) if (k !== 'set-cookie') h[k] = v;
  const sc = r.headers.getSetCookie(); if (sc.length) h['set-cookie'] = sc;
  res.writeHead(r.status, h); res.end(Buffer.from(await r.arrayBuffer()));
}).listen(PORT, '127.0.0.1', () => console.log(`dev portal http://127.0.0.1:${PORT} -> ${env.ORIGIN_URL}`));
