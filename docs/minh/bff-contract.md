# Hợp đồng BFF–origin (C04 ↔ B05)

Phiên bản 0.1 — đề xuất, **chưa được Toàn/Phát xác nhận**. Mã: `deploy/cloudflare/src/worker.js`.

## Phía Worker (Minh) — đã viết, đã có unit test (`npm test` trong `deploy/cloudflare`)
| Hạng mục | Hành vi |
|---|---|
| Đích | Chỉ `ORIGIN_URL` (var). Không nhận URL/path nội bộ từ client. |
| Route | Allowlist method + path khớp bảng route `ott/api.py`. Ngoài allowlist: 404/405 JSON. `/api/v1/stream`: 404 `STREAM_DISABLED`. |
| Header chuyển tiếp | `accept, accept-language, content-type, cookie, origin, x-csrf-token, user-agent`. Mọi header khác (x-forwarded-*, host, cf-*, `x-contest-origin-key` do client gửi) bị bỏ. |
| Key | Luôn gắn `X-Contest-Origin-Key: <secret ORIGIN_KEY>`; thêm `X-Request-ID`. |
| Mutation | Bắt buộc `Origin` == `PUBLIC_ORIGIN` (kể cả login/register); body ≤ 1 MiB tính theo byte khi đọc (413), chỉ `application/json` (415). |
| Redirect / retry | `redirect: manual`; origin trả 3xx → 502 `ORIGIN_REDIRECT`. Không tự retry (nhất là POST). Timeout 25 giây → 504. |
| Phản hồi origin | Không phải JSON (trang lỗi tunnel, index.html) → 502/503 JSON, không chuyển cho client. |
| Cookie | Giữ `Set-Cookie`, **bỏ thuộc tính Domain** để host-only. |
| Cache | Mọi `/api/*` và `/runtime-config.js`: `Cache-Control: no-store`. |
| Lỗi | `{ "error": { "code", "message", "request_id" } }`; 429/503 kèm `Retry-After` khi có. |

## Phía backend (Toàn, B05) — **chưa có trong repo, cần làm trước khi demo chạy được mutation**
Đã kiểm tra trên commit `370e604`: qua Worker, đăng nhập và đọc dữ liệu chạy được, nhưng mọi mutation sau đăng nhập trả `403 FORBIDDEN "Cross-origin request rejected."` vì `ott/api.py` so `Origin` với `Host` của kết nối tới tunnel.

1. Đọc `ORIGIN_KEY` từ cấu hình; thiếu/sai key → từ chối (so sánh constant-time) trước mọi nghiệp vụ, kể cả `GET` và `/stream` cũ.
2. Biến `PUBLIC_ORIGIN` cố định; mutation so `Origin` đúng scheme/host/port với biến này (thay vì `Host`). Giữ kiểm tra CSRF token.
3. `MAX_BODY` hiện 400 KiB; nâng lên 1 MiB cùng source ≤ 256 KiB để khớp Worker (mục 7.3 hồ sơ).
4. Chạy `--secure-cookie` (cookie `Secure`) khi sau portal HTTPS.
5. Backend bind `127.0.0.1`; Quick Tunnel trỏ vào cổng loopback.

## Cần Minh/Toàn chốt
- `PUBLIC_ORIGIN` thật (sau lần `wrangler deploy` đầu).
- Cách xoay `ORIGIN_KEY`: backend nhận hai key trong cửa sổ ngắn (xem mục 15.6 hồ sơ).
- Idempotency cho POST (hiện Worker không retry nên chưa cần).
