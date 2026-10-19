# Runbook demo qua Cloudflare Worker + Quick Tunnel (C07)

Trạng thái: bản nháp, **chưa diễn tập trên Cloudflare thật**. Lệnh `wrangler`/`cloudflared` chưa được chạy trong quá trình soạn.

## 0. Một lần
1. `cd deploy/cloudflare && npm install`
2. `npx wrangler login`; `npx wrangler secret put ORIGIN_KEY` (chuỗi ngẫu nhiên ≥ 32 ký tự; lưu ở nơi quản lý secret, không đưa vào Git).
3. `npx wrangler deploy` → ghi lại URL `https://ott-arena-demo.<sub>.workers.dev`, điền vào `PUBLIC_ORIGIN` trong `wrangler.toml` rồi deploy lại.

## 1. Trước phiên (khớp mục 8 hồ sơ: trước 30 phút)
1. Trong Linux VM: backend `python -m ott serve --host 127.0.0.1 --port 8080 --runner docker --no-register --secure-cookie` (kèm cấu hình `ORIGIN_KEY`/`PUBLIC_ORIGIN` của B05).
2. Mở tunnel: `cloudflared tunnel --url http://127.0.0.1:8080` → lấy URL `https://xxxx.trycloudflare.com`.
3. `./scripts/set-origin.sh https://xxxx.trycloudflare.com`
4. `PORTAL_URL=<portal> ORIGIN_URL=<tunnel> OTT_USER=<tk thử> OTT_PASS=<mk> node smoke.mjs` → mọi dòng PASS; lưu file trong `evidence/`.
5. Mở portal bằng một máy khác: đăng nhập, mở trang giải, thử một mutation.

## 2. Trong phiên
- Footer hiện "Cập nhật mỗi 10 giây" (không có SSE). Không hứa cập nhật tức thời.
- Theo dõi quota Workers (Dashboard → Workers → Metrics): cảnh báo 70%, từ 85% giảm hoạt động không thiết yếu (mục 7.5).
- Mô hình: 30 client × 1 request/10 giây × 4 giờ ≈ 43.200 request (chưa gồm login, nộp bài, autosave). Mỗi trang có thể có thêm poll riêng; đo thực tế ở AC12.

## 3. Mất Tunnel / đổi URL (AC11)
1. Client tự backoff (429/502/503/504 → chờ, ưu tiên `Retry-After`); người dùng thấy lỗi kết nối, không bị retry dồn.
2. Toàn kiểm `cloudflared` và backend; nếu tunnel mới: tạo lại, lấy URL mới.
3. Minh chạy `./scripts/set-origin.sh <URL mới>` (không sửa web, không cần người dùng đổi link).
4. Chạy lại `smoke.mjs` (đăng nhập + một mutation) trước khi báo mở lại.
5. Ghi sự cố: ID, thời điểm UTC+7, phạm vi, người quyết định, mốc thông báo tiếp theo.
Không hướng người dùng nộp vào URL tunnel trực tiếp.

## 4. Nghi lộ `ORIGIN_KEY`
Toàn dừng connector; đặt key mới (`wrangler secret put ORIGIN_KEY`, cập nhật backend); chạy `smoke.mjs`; mở lại sau khi xác nhận.

## 5. Diễn tập không cần Cloudflare
```
python -m ott seed && python -m ott serve --runner local --port 8099 --no-register --secure-cookie
cd deploy/cloudflare && ORIGIN_URL=http://127.0.0.1:8099 node dev-server.mjs     # portal giả lập http://127.0.0.1:8787
PORTAL_URL=http://127.0.0.1:8787 OTT_USER=team_alpha OTT_PASS=... node smoke.mjs
```
`dev-server.mjs` chạy đúng `worker.js` nhưng không phải môi trường Cloudflare thật.

## 6. Kết thúc
Đóng nhận bài, lưu `evidence/`, dừng `cloudflared`, `wrangler delete` nếu không giữ demo.
