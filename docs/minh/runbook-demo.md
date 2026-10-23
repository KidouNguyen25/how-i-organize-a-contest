# Runbook demo qua Cloudflare Worker + Quick Tunnel (C07)

Trạng thái: bản nháp, **chưa diễn tập trên Cloudflare thật**. Lệnh `wrangler`/`cloudflared` chưa được chạy trong quá trình soạn.

## 0. Một lần
1. `cd deploy/cloudflare && npm ci` (nếu chưa có lockfile, dùng `npm install`). Sử dụng **`wrangler.toml` và `src/worker.js`**, không trộn với cấu hình `wrangler.jsonc` của gateway cũ. Mọi lệnh Wrangler bên dưới truyền `--config wrangler.toml`.
2. `npx wrangler login`; `npx wrangler secret put ORIGIN_KEY --config wrangler.toml` (khóa ngẫu nhiên ASCII ≥ 32 ký tự; lưu tại secret manager, không đưa vào Git).
3. Deploy lần đầu bằng `npx wrangler deploy --config wrangler.toml` để lấy origin HTTPS chính xác. Đặt `PUBLIC_ORIGIN` đúng URL đó trong `wrangler.toml` rồi deploy lại. Trên backend cấu hình `CONTEST_PUBLIC_ORIGIN` trùng giá trị và `CONTEST_ORIGIN_KEY` trùng secret Worker; hai bên phải được cấu hình trước nghiệm thu.

## 1. Trước phiên (khớp mục 8 hồ sơ: trước 30 phút)
1. Trong Linux VM: cấu hình `CONTEST_ORIGIN_KEY` và `CONTEST_PUBLIC_ORIGIN` từ secret/config không lưu vào Git; backend `python -m ott serve --host 127.0.0.1 --port 8080 --profile demo --runner docker --runtime-image ott-bot-runtime --source-allowlist data/approved-sources.json --no-register --secure-cookie`. Trước khi chạy phải build hai Docker image và tạo source allowlist theo `deploy/README.md`.
2. Mở tunnel: `cloudflared tunnel --url http://127.0.0.1:8080` → lấy URL `https://xxxx.trycloudflare.com`.
3. `./scripts/set-origin.sh https://xxxx.trycloudflare.com`
4. `PORTAL_URL=<portal HTTPS> ORIGIN_URL=<tunnel HTTPS> ORIGIN_KEY=<secret> OTT_USER=<tk thử> OTT_PASS=<mk> node smoke.mjs` → bắt buộc W01–W08, O01–O02, A01–A03 đều PASS; nếu thiếu tài khoản hoặc `ORIGIN_URL` thì biên bản chưa đủ điều kiện nghiệm thu. Lưu file trong `evidence/` ở kho bảo mật phù hợp; không commit secret.
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
Đóng truy cập gateway; tạo secret mới bằng `npx wrangler secret put ORIGIN_KEY --config wrangler.toml`, cập nhật `CONTEST_ORIGIN_KEY` tại backend. Chỉ giữ `CONTEST_ORIGIN_KEY_PREVIOUS` trong cửa sổ chuyển đổi được phê duyệt, sau đó xóa. Chạy lại smoke trên portal và origin trước khi mở lại.

## 5. Diễn tập không cần Cloudflare
```
python -m ott seed && python -m ott serve --runner local --port 8099 --no-register --secure-cookie
cd deploy/cloudflare && ORIGIN_URL=http://127.0.0.1:8099 node dev-server.mjs     # portal giả lập http://127.0.0.1:8787
PORTAL_URL=http://127.0.0.1:8787 OTT_USER=team_alpha OTT_PASS=... node smoke.mjs
```
`dev-server.mjs` chạy đúng `worker.js` nhưng không phải môi trường Cloudflare thật.

## 6. Kết thúc
Đóng nhận bài, lưu `evidence/`, dừng `cloudflared`. Chỉ xóa Worker khi đã phê duyệt và không còn sử dụng; lệnh `npx wrangler delete --config wrangler.toml` là thao tác phá hủy dịch vụ.

## 7. Tiêu chí nghiệm thu chưa được thực hiện
- Cloudflare thật, Linux VM thật, Docker/cgroup, firewall thực tế, backup/restore và tải 30 client **chưa có log xác nhận** trong repository.
- Theo dõi trạng thái G3/G4/B08 và C05/C06/C08 tại `docs/release-acceptance.md`. Chỉ gắn nhãn hoàn tất khi có evidence gắn SHA và cấu hình đã được soát lọc thông tin nhạy cảm.
