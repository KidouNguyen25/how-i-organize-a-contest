# Hợp đồng BFF–origin (C04 ↔ B05)

Phiên bản 0.2 — đối chiếu với `ott/security.py`, `ott/api.py`, `ott/__main__.py` và `deploy/cloudflare/src/worker.js` trên `main` ngày 23/10/2026. **Đối chiếu mã nguồn, chưa nghiệm thu Cloudflare/Tunnel thật.**

## Phía Worker (Minh) — đã viết, đã có unit test (`npm test` trong `deploy/cloudflare`)
| Hạng mục | Hành vi |
|---|---|
| Đích | Chỉ `ORIGIN_URL` (var). Không nhận URL/path nội bộ từ client. |
| Route | Allowlist method + path khớp bảng route `ott/api.py`. Ngoài allowlist: 404/405 JSON. `/api/v1/stream`: 404 `STREAM_DISABLED`. |
| Header chuyển tiếp | `accept, accept-language, content-type, cookie, origin, x-csrf-token, user-agent, sec-fetch-site`. Mọi header khác (x-forwarded-*, host, cf-*, `x-contest-origin-key` do client gửi) bị bỏ. |
| Key | Luôn gắn `X-Contest-Origin-Key: <secret ORIGIN_KEY>`; thêm `X-Request-ID`. |
| Mutation | Bắt buộc `Origin` == `PUBLIC_ORIGIN` (kể cả login/register); body ≤ 1 MiB tính theo byte khi đọc (413), chỉ `application/json` (415). |
| Redirect / retry | `redirect: manual`; origin trả 3xx → 502 `ORIGIN_REDIRECT`. Không tự retry (nhất là POST). Timeout 25 giây → 504. |
| Phản hồi origin | Không phải JSON (trang lỗi tunnel, index.html) → 502/503 JSON, không chuyển cho client. |
| Cookie | Giữ `Set-Cookie`, **bỏ thuộc tính Domain** để host-only. |
| Cache | Mọi `/api/*` và `/runtime-config.js`: `Cache-Control: no-store`. |
| Lỗi | `{ "error": { "code", "message", "request_id" } }`; 429/503 kèm `Retry-After` khi có. |

## Phía backend (B05) — đã đối chiếu source, chưa có acceptance staging

Báo cáo lỗi 403 trên commit `370e604` là kết quả lịch sử; không được dùng để mô tả hành vi hiện tại. Mã nguồn hiện tại đã thay đổi:

1. `ott/__main__.py` đọc `CONTEST_ORIGIN_KEY` và `CONTEST_ORIGIN_KEY_PREVIOUS`; `ServerPolicy.authenticate()` trong `ott/security.py` kiểm tra secret (constant-time) cả khi đọc và ghi. Không cho phép proxy mode chỉ cấu hình một nửa `CONTEST_PUBLIC_ORIGIN` / `CONTEST_ORIGIN_KEY`.
2. Mutation so `Origin` đúng chính xác với `CONTEST_PUBLIC_ORIGIN`, không so với `Host` của tunnel. HTTP mutation vẫn kiểm tra CSRF sau khi xác thực; `Sec-Fetch-Site: cross-site` bị từ chối.
3. `MAX_BODY = 1 MiB`, `MAX_SOURCE = 256 KiB` trong `ott/api.py`; `ServerPolicy.max_body` cùng trần 1 MiB. Worker tự giới hạn stream body 1 MiB.
4. Chế độ có `origin_keys` tự bật cookie `Secure` tại `App.__init__`. Session cookie còn có `HttpOnly; SameSite=Lax; Path=/`; Worker loại thuộc tính `Domain`.
5. CLI hạn chế `--host` thành `127.0.0.1` hoặc `localhost`; origin sau Quick Tunnel phải trỏ tới loopback.

Unit test hồi quy tại `tests/test_gateway_contract.py` kiểm tra các ràng buộc Python. Worker unit test tại `deploy/cloudflare/test/worker.test.mjs` kiểm tra phía BFF. **Chưa được phép đánh dấu C06/C08 đạt** chỉ từ việc đọc source hay chạy unit test.

### Điều kiện nghiệm thu còn thiếu

- Cấu hình `CONTEST_PUBLIC_ORIGIN` và `PUBLIC_ORIGIN` trùng URL Worker HTTPS thật; key ≥ 32 ký tự, khai báo bằng secret manager, không commit.
- Chạy `smoke.mjs` trên portal staging, có `OTT_USER` và `OTT_PASS` thử nghiệm; lưu JSON đã loại secret. Test O01/O02 và A01–A03 phải đạt, không chỉ W01–W08.
- Xác nhận cookie qua trình duyệt thật, polling và `Retry-After`; tải 30 client, gián đoạn tunnel và rollback phải có biên bản riêng.
- Gắn commit SHA, bản cấu hình không nhạy cảm, thời gian UTC và người nghiệm thu với mọi kết quả; không tuyên bố đạt nếu test bị skip.

## Quyết định vận hành cần ghi lại
- URL `PUBLIC_ORIGIN` thật sau `wrangler deploy` (đồng bộ với `CONTEST_PUBLIC_ORIGIN`).
- Thời gian cho phép `CONTEST_ORIGIN_KEY_PREVIOUS` khi xoay key; bỏ key cũ sau cửa sổ có kiểm soát.
- Worker không tự retry POST; chưa được khẳng định POST idempotent. Cần thêm kiểm thử mất mạng giữa lúc origin chấp nhận yêu cầu và lúc client nhận phản hồi.
