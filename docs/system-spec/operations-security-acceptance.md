# Bảo mật, vận hành và nghiệm thu

Phiên bản: 1.0
Ngày: 27/10/2026

Đối tượng: người vận hành và người duyệt release.

## 1. Mô hình triển khai

### Development

Chạy local để phát triển giao diện, API và luật. `--runner local` chạy mã bot như process thường trên host, không phải sandbox bảo mật. Chỉ dùng source tin cậy.

```sh
python -m ott seed --data data
python -m ott serve --profile development --runner local --host 127.0.0.1 --port 8080
```

### Demo qua Docker và gateway

Demo yêu cầu Linux Docker, source allowlist, compiler image và runtime image riêng. API bind loopback; Cloudflare Worker/Quick Tunnel là điểm public. Demo không nhận source tùy ý từ Internet. Demo profile giới hạn worker theo cấu hình hiện hành và tắt SSE để dùng polling.

```sh
python -m ott build-image --target compiler --image ott-bot-runner
python -m ott build-image --target runtime --image ott-bot-runtime
python -m ott source-hashes --normalize-newlines samples/bot_template.cpp samples/bot_template.c samples/bot_template.rs samples/bot_template.go samples/bot_template.java > data/approved-sources.json
python -m ott serve --profile demo --runner docker --runtime-image ott-bot-runtime --source-allowlist data/approved-sources.json --host 127.0.0.1 --port 8080
```

Trước khi dùng lệnh, rà source allowlist, image tag/base digest, quyền Docker, secret và firewall theo [deploy/README.md](../../deploy/README.md). Không commit secret hoặc file tài khoản mẫu.

## 2. Sandbox và ranh giới bảo mật

- Compiler và runtime chạy trong container tách biệt. Bot chạy non-root, không mạng, root filesystem chỉ đọc, bỏ capability, bật `no-new-privileges`, giới hạn CPU/RAM/PID/time/output và xóa container sau khi chạy.
- Mặc định README ghi runtime 1 CPU, 256 MiB RAM và 64 PID; compile 1 CPU, 1 GiB RAM, timeout 15 giây, Go 30 giây. Chi tiết từng ngôn ngữ/image phụ thuộc image đã build.
- Artifact cần khớp source hash, ngôn ngữ, manifest và image ID được profile cho phép. Không bỏ kiểm tra hoặc thay binary sau khi roster đã khóa.
- Web session dùng cookie `Secure`, `HttpOnly`, `SameSite=Lax`; mutation dùng CSRF. Gateway gắn origin key; backend xác thực key và public origin.
- Worker allowlist route/header, giới hạn body 1 MiB, không tự retry mutation và không theo redirect origin. Origin chỉ bind loopback.
- Người có quyền Docker daemon/root có thể kiểm soát host. Container chia sẻ kernel; đây là lớp giảm rủi ro, không phải VM.
- `judge/` TCP legacy có TLS và Docker nhưng không kế thừa toàn bộ quota, deadline, rate limit hay role controls của OTT web API. Không coi hai dịch vụ có cùng security posture.

## 3. Điểm vận hành trọng yếu

- Không expose cổng API OTT 8080 trực tiếp. Dùng reverse proxy/gateway đã cấu hình origin secret và HTTPS.
- `--runner local` chỉ dành cho development. Demo cần Docker và source allowlist không rỗng.
- `--no-dispatch` tách match dispatcher khỏi API; compiler chưa được tách thành tiến trình độc lập. Endpoint `code/check` compile đồng bộ.
- Sau lỗi hạ tầng khi bot đang chạy, trận có thể chuyển sang `PAUSED`/review. Bot RAM không được khôi phục giữa trận; không tuyên bố resume liên tục.
- Hai bộ Cloudflare config cũ/mới cùng tồn tại. Runbook yêu cầu dùng `deploy/cloudflare/src/worker.js` cùng `wrangler.toml`, không trộn với `worker.mjs`/`wrangler.jsonc` cũ.
- Firewall profile ban đầu có egress allowlist rỗng. Điền và rà endpoint DNS/NTP/tunnel trước khi áp trên VM; cần console và rollback. Không dùng `flush ruleset`.
- Release manifest kiểm tra snapshot cấu hình/artifact; không chứng minh load test, firewall, restore hay acceptance đã đạt.

## 4. Backup, migration và phục hồi

Backup chỉ chạy khi API, dispatcher, compiler và phiên bản cũ đã dừng. Lệnh yêu cầu xác nhận offline:

```sh
python -m ott backup --data data --destination ../contest-backup --offline
python -m ott verify-backup ../contest-backup
python -m ott restore ../contest-backup --destination ../contest-restore
python -m ott migrate-copy ../contest-backup --destination ../contest-migrated
```

Restore/migrate yêu cầu đích mới chưa tồn tại. Sau thao tác, kiểm tra row count, hash, quyền file, schema và khả năng khởi động trên bản sao. Bảo vệ backup bằng quyền tối thiểu và mã hóa nơi lưu ngoài host. Không thử migration lần đầu trên database duy nhất.

Khi deploy image mới, xử lý receipt đang chờ, backup DB/artifact, build compiler/runtime image, kiểm tra digest rồi triển khai API và worker đồng bộ. Image ID đổi có thể làm artifact cũ không còn hợp lệ; cần quy trình release có kiểm soát.

## 5. Cổng nghiệm thu còn mở

Theo `docs/release-acceptance.md` (cập nhật 23/10/2026):

| Cổng | Trạng thái hồ sơ | Bằng chứng còn cần |
|---|---|---|
| Compiler isolation/recovery | OPEN | Compiler worker độc lập hoặc bounded isolated path; crash/requeue/lease/fencing/retry/quota tests trên Linux Docker. |
| Core và OTT integration | OPEN | Một persistent repository/controller dispatch CP và OTT; chạy hai mùa liên tiếp, restart và kiểm tra phân quyền/data isolation. |
| Cloudflare BFF staging | OPEN | Deploy Worker thật; smoke đủ W01–W08, O01–O02, A01–A03; kiểm cookie/CSRF, retry-after, outage/recovery và tải thực. |
| Linux staging/release | OPEN | VM riêng; Docker/cgroup, firewall thật, backup/restore/migrate-copy, rollback và tải đại diện được ghi nhận. |

Trạng thái `OPEN` trong hồ sơ không đồng nghĩa implementation không có. Nó có nghĩa release evidence chưa đủ. Unit test, đọc source, cấu hình mẫu hoặc `release-manifest` không thay thế bằng chứng staging.

## 6. Hồ sơ bằng chứng

Ghi tối thiểu cho mỗi scenario: issue, scenario ID, thời điểm UTC, git commit, môi trường, hash cấu hình đã loại secret, image digest, command, result, artifact hash và reviewer. Lưu log/screenshot ở vùng hạn chế; loại bỏ password, session cookie, source riêng tư và origin key. Ghi `NOT_RUN` hoặc `BLOCKED` riêng; không nâng thành `PASS`.

## 7. Danh sách kiểm tra trước demo

- [ ] Xác nhận source allowlist, compiler/runtime image ID và source bot đã duyệt.
- [ ] Xác nhận API chỉ bind loopback; gateway URL, public origin, origin key và cookie đúng.
- [ ] Xác nhận Linux Docker, quota container, DNS/NTP/tunnel egress và dashboard vận hành.
- [ ] Tạo và xác minh backup; diễn tập restore/migrate trên thư mục mới.
- [ ] Chạy đủ smoke test và kiểm tra đăng nhập, mutation, CSRF, nộp bot, trận, polling và lỗi tunnel.
- [ ] Ghi kết quả thật, commit, image digest, cấu hình đã lọc secret và người duyệt.

Tài liệu vận hành chi tiết: [deploy/README.md](../../deploy/README.md), [runbook demo](../minh/runbook-demo.md), [BFF contract](../minh/bff-contract.md), [languages and security](../languages-security.md).
