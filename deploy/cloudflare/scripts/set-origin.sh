#!/usr/bin/env bash
# Đổi origin sau khi Quick Tunnel đổi URL (AC11). Không cần sửa browser hay build lại web.
# Dùng: ./scripts/set-origin.sh https://xxxx.trycloudflare.com
set -euo pipefail
url="${1:-}"
[[ "$url" =~ ^https://[a-z0-9-]+\.trycloudflare\.com/?$ ]] || { echo "URL phải dạng https://<tên>.trycloudflare.com" >&2; exit 2; }
cd "$(dirname "$0")/.."
npx wrangler deploy --var "ORIGIN_URL:${url%/}"
echo "Đã đổi ORIGIN_URL. Chạy smoke: PORTAL_URL=<url portal> node smoke.mjs"
