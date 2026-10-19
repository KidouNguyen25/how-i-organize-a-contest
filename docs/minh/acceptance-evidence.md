# Bằng chứng nghiệm thu phần Minh (C04–C06)

Môi trường: sandbox Linux, Node v22, Python 3 (`ott serve --runner local`), commit gốc `370e604` + thay đổi trong gói này. **Không phải Cloudflare thật, không có Docker sandbox, không có Quick Tunnel.**

| Mã | Kịch bản | Cách chạy | Kết quả thực tế | Trạng thái |
|---|---|---|---|---|
| C04/AC08 (Worker) | Allowlist, stream bị chặn, ghi đè origin key, Origin giả → 403, 413, redirect, HTML từ origin, cookie bỏ Domain, no-store, không lộ key | `cd deploy/cloudflare && npm test` | 27/27 test đạt (gồm 7 test `net.js`) | Đạt (unit) |
| C05/AC11 | Poll 10 giây ở chế độ tunnel, tab ẩn dừng và chạy bù, backoff theo Retry-After, không chạy chồng | cùng lệnh trên | Đạt (unit, đồng hồ giả) | Đạt (unit); chưa đo trên trình duyệt thật |
| C06 (smoke) | Chạy `smoke.mjs` qua `dev-server.mjs` → backend local `--secure-cookie` | xem runbook mục 5 | 10/11 đạt. A03 hỏng: logout đúng CSRF vẫn 403 `Cross-origin request rejected` do backend so Origin với Host | **Chưa đạt** — chờ B05 phía backend |
| AC08 (origin trực tiếp) | O01/O02 gọi thẳng origin không key | `smoke.mjs` với `ORIGIN_URL` | Chưa chạy: backend chưa có kiểm tra key | Chưa chạy |
| AC12 | 30 client, đo request/quota | — | Chưa chạy | Chưa chạy |
| AC16 | Người khác làm theo runbook | — | Chưa chạy | Chưa chạy |

Không đánh dấu Done cho C04–C07 cho tới khi chạy được trên Cloudflare thật và A03/O01/O02 đạt.
