# Chạy phần worker, sandbox và vận hành

Các lệnh dưới đây dành cho người vận hành. Chưa áp firewall hoặc triển khai dịch vụ khi thêm các file này.

## Phạm vi hiện tại

- SQLite schema 2 có migration từ schema 1, lease, fencing, attempt và khóa tài nguyên dùng chung.
- OTT đi qua registry adapter. Transport và schema trận vẫn dùng hai bên white/black; chưa thay thế contract đa loại giải của phần A05.
- Source cuối được lưu bền vững trước biên dịch, trả HTTP 202 và giữ thời điểm nhận. Theo dõi `GET /api/v1/tournaments/{id}/code`, trường `pending_submission`.
- API và match dispatcher có thể chạy riêng trên cùng Linux VM. Compiler vẫn được gọi từ API; `--no-dispatch` không tạo ranh giới cách ly compiler khỏi API.
- Demo Internet chỉ cho source nằm trong allowlist. Chưa đủ điều kiện nhận code bất kỳ từ Internet.

## Chuẩn bị và chạy local

Yêu cầu Python 3.11 trở lên. Demo Docker cần Linux containers và Docker daemon. Chạy từ checkout dự án; systemd dùng checkout tại `/opt/contest`.

Trước khi đổi DB cũ, dừng mọi API, worker, compiler và phiên bản cũ; tạo backup. Không chạy migration trực tiếp trên bản dữ liệu duy nhất.

```sh
python -m ott backup --data data --destination ../contest-backup-before-v2 --offline
python -m ott migrate-copy ../contest-backup-before-v2 --destination ../contest-data-v2
python -m ott verify-backup ../contest-backup-before-v2
```

Nếu chưa có DB, tạo tài khoản mẫu. Mật khẩu ngẫu nhiên chỉ ghi vào `data/seed-accounts.json`; file này không vào backup.

```sh
python -m ott seed --data data
python -m ott build-image --image ott-bot-runner
python -m ott build-image --target runtime --image contest-bot-runtime
```

`build-image --base-image name@sha256:digest` cho phép khóa base image đã được duyệt. Runtime luôn dùng image ID thực đọc từ Docker, không chạy theo tag đang thay đổi.

BTC rà source mẫu rồi tạo allowlist trên Linux:

```sh
python -m ott source-hashes --normalize-newlines samples/bot_template.cpp > data/approved-sources.json
python -m ott serve --profile demo --data data --runner docker --runtime-image contest-bot-runtime --source-allowlist data/approved-sources.json
```

Trên PowerShell, ghi kết quả JSON với UTF-8, không dùng chuyển hướng tạo UTF-16. `--normalize-newlines` chọn dạng LF mà textarea gửi trước khi BTC duyệt hash; nếu bỏ cờ này, hash giữ nguyên byte của file. Không chuẩn hóa source sau khi đã nhận và tính hash.

Phát triển không có Docker phải chọn rõ `--profile development --runner local`. CLI chỉ bind IPv4 loopback. `auto` không tự chuyển sang local khi Docker lỗi.

Artifact cũ thiếu manifest sẽ bị chặn. Chuẩn bị bản nộp mới của source đã duyệt trong mùa demo; không xóa manifest hoặc âm thầm thay binary đã thi đấu.

## Worker riêng và dừng an toàn

Trên Linux, chạy API với `--no-dispatch` và tiến trình thứ hai cùng profile/data/image/allowlist:

```sh
python -m ott worker --profile demo --data data --runner docker --runtime-image contest-bot-runtime --source-allowlist data/approved-sources.json
```

Hai tiến trình giữ shared lock dữ liệu; backup cần exclusive lock nên bị chặn khi runtime còn chạy. API có thêm exclusive role lock để chỉ có một API nhận bài. Windows dùng khóa bảo thủ độc quyền, vì vậy chạy chế độ gộp hoặc chuyển sang Linux VM.

SIGTERM/Ctrl+C ngừng nhận việc và chờ writer kết thúc trước khi nhả lock. Trận đã khởi động bot nhưng gặp lỗi hạ tầng chuyển PAUSED để review; chưa hỗ trợ phục hồi RAM bot. Retry trận trước khi bot bắt đầu tối đa hai attempt. Receipt build giữ nguồn và quota qua restart; bộ đếm lỗi hạ tầng build chỉ tính lỗi đã ghi, chưa phải ledger mọi lần compiler crash.

## Gateway Cloudflare

`cloudflare/worker.mjs` chỉ phục vụ assets và chuyển API allowlist. Đích chỉ nhận hostname Quick Tunnel. Không dùng SSE, không cache API hoặc theo redirect upstream. Gateway không chạy mã thí sinh.

1. Tạo secret ngẫu nhiên mạnh từ 32 ký tự ASCII trở lên. Đặt cùng giá trị tại backend `CONTEST_ORIGIN_KEY` và Wrangler secret `ORIGIN_KEY`. Không dùng giá trị placeholder.
2. Đặt `CONTEST_PUBLIC_ORIGIN` ở backend; cập nhật `PUBLIC_ORIGIN` trong Wrangler bằng public origin thực, không có dấu `/` cuối.
3. Mở Quick Tunnel tới `http://127.0.0.1:8080`; cập nhật `ORIGIN_URL` trong Wrangler.
4. Dùng `npx wrangler secret put ORIGIN_KEY --config deploy/cloudflare/wrangler.jsonc`. Việc publish Worker là bước vận hành riêng.
5. Khi đổi Tunnel URL, chỉ cập nhật `ORIGIN_URL`; frontend vẫn gọi cùng public origin.

Origin key bảo vệ mọi route origin. Session, quyền và CSRF vẫn được kiểm riêng. `CONTEST_ORIGIN_KEY_PREVIOUS` hỗ trợ xoay key trong cửa sổ ngắn. Secret không nằm trong browser bundle, URL hoặc manifest release.

Polling màn hình nộp bài dùng 10 giây và dừng tab ẩn. Polling tổng hợp toàn portal, backoff và nghiệm thu tải thuộc phần C05/C06; chưa coi cấu hình này là demo đã đạt quota hoặc tải 30 đội.

## Firewall và systemd

`firewall.nft` là profile cho Linux VM riêng: chặn inbound mới, chặn forwarding mới, chỉ cho outbound DNS/NTP/HTTPS/Tunnel theo cổng. Không mở SSH công khai, API, DB hoặc Docker TCP. Rule inet áp cả IPv4/IPv6 và chặn forwarding trước chain Docker. Không phụ thuộc UFW để bảo vệ published ports.

Rà interface, DNS/NTP, DHCP và đích Cloudflare trên VM thật. Profile chưa giới hạn IP đích outbound; người vận hành phải bổ sung danh sách được duyệt. Có console và bản rollback trước khi áp. Dùng `nft -c -f deploy/firewall.nft` để kiểm cấu hình trước; rollback chỉ xóa bảng `inet contest_demo`. Không flush toàn bộ ruleset. Image build qua Docker bridge có thể bị chặn, nên chuẩn bị image trước cửa sổ demo.

`contest.service` dùng tài khoản vận hành tin cậy `contest`, checkout `/opt/contest`, data `/srv/contest/data`, workspace `/srv/contest/work`, môi trường `/etc/contest/demo.env` và allowlist `/etc/contest/source-allowlist.json`. Tạo các đường dẫn/quyền tương ứng trước khi cài service. `TMPDIR` phải nhìn thấy được từ Docker daemon, vì vậy không đặt bind source trong private `/tmp` của systemd. Quyền nhóm Docker vẫn tương đương đặc quyền host; chỉ dùng VM chuyên biệt và source mẫu đã duyệt.

## Maintenance, readiness và dữ liệu

```sh
python -m ott maintenance --data data --on
python -m ott maintenance --data data --off
python -m ott backup --data data --destination ../backup-new --offline
python -m ott restore ../backup-new --destination ../restore-new
python -m ott release-manifest --data data --runner docker --source-allowlist data/approved-sources.json --output data/release.json
```

Maintenance chỉ ngừng mutation/job mới; không thay thế dừng toàn bộ runtime để backup. `restore` và `migrate-copy` chỉ nhận đường dẫn chưa tồn tại, không ghi đè DB hoặc bài mới. Gói backup có source/draft, hash mật khẩu và binary; bảo vệ bằng quyền tối thiểu và mã hóa ở nơi lưu ngoài máy. Manifest kiểm tra toàn vẹn, không phải chữ ký xác thực. Chưa triển khai migration PostgreSQL.

`/healthz` kiểm tiến trình; `/readyz` kiểm DB/schema, disk, maintenance và runtime với cache ngắn. Qua origin bảo vệ, gửi key bằng header riêng. Organizer đọc `/api/v1/ops/status` để xem queue, attempt, lease và checkpoint; `/api/v1/ops/maintenance` cần session và CSRF.

Release manifest ghi commit, trạng thái working tree, schema, image ID, hash allowlist và cấu hình dự kiến. Đây không phải bằng chứng các test/cgroup/firewall/restore đã đạt. Rollback ứng dụng phải tương thích schema; nếu có bài mới sau cutover, giữ DB mới và đối soát trước khi quyết định chuyển ngược.

Nguồn cấu hình: [Cloudflare assets](https://developers.cloudflare.com/workers/static-assets/binding/), [Docker resource constraints](https://docs.docker.com/engine/containers/resource_constraints/), [Docker firewall](https://docs.docker.com/engine/network/packet-filtering-firewalls/), [SQLite Backup API](https://www.sqlite.org/backup.html).
