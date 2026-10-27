# Kiến trúc và hợp đồng kỹ thuật

Phiên bản: 1.0

Phạm vi: kiến trúc có trong mã nguồn tại `origin/main`; các ranh giới thiết kế và thiếu hụt tích hợp được nêu rõ.

## 1. Bản đồ thành phần

```text
Trình duyệt
  ├─ mockup/ web client ── HTTPS same-origin ── Cloudflare Worker (khi dùng gateway)
  │                                               └─ origin key ── OTT HTTP API
  └─ Không kết nối TCP judge trực tiếp

OTT HTTP API (ott/api.py)
  ├─ auth, validation, CSRF và role checks (ott/security.py)
  ├─ SQLite store (ott/store.py)
  ├─ scheduler/dispatcher (ott/tournament.py)
  ├─ engine luật (ott/engine.py)
  ├─ bot runner (ott/runner.py)
  └─ compiler/runtime sandbox (ott/sandbox.py, ott/docker/)

Judge legacy: TCP/TLS server (judge/server.py) ── judge engine ── SQLite riêng
Generic Core v2: core/api_v2.py + adapters; controller hiện giữ state trong RAM
```

OTT Arena là đường chạy chính cho giải bot. `judge/` là dịch vụ chấm test tĩnh qua TCP/TLS. `core/` mô tả kiến trúc cuộc thi generic nhưng chưa làm lớp persistent dùng chung với OTT; không coi sơ đồ module là bằng chứng các hệ thống đã hợp nhất.

## 2. Trách nhiệm module

| Module | Trách nhiệm | Ghi chú |
|---|---|---|
| `mockup/` | SPA JavaScript, màn hình thí sinh/organizer, soạn bot, bàn cờ, lịch, standings. | Tên thư mục cũ; README xác nhận client gọi API thật. Không cần build step. |
| `ott/api.py` | HTTP `/api/v1`, static assets, auth, tournament/code/match endpoints, SSE. | API thực thi là nguồn đối chiếu cho contract hiện tại. |
| `ott/security.py` | Origin key, Origin/CSRF checks, cookie/session policy và bảo vệ request. | Public deployment cần gateway được cấu hình đồng bộ. |
| `ott/store.py` | SQLite, transaction, schema migration, users, tournaments, entries, bot submissions, receipts, matches và events. | Persistent store riêng của OTT. |
| `ott/engine.py` | Pure rules engine cho OTT v1, state transition, legal actions và kết quả. | Engine xác thực action; không tin dữ liệu do bot gửi ngoài action. |
| `ott/tournament.py` | Double round robin, standings, dispatch và quản lý vòng đời match. | Demo profile giới hạn một worker. |
| `ott/runner.py`, `ott/sandbox.py` | Compile/run bot, giới hạn process, kiểm tra artifact và Docker isolation. | Local runner chỉ dành cho development với source tin cậy. |
| `ott/events.py` | Hub SSE cho thay đổi API. | Demo/tunnel có thể tắt stream và dùng polling. |
| `ott/backup.py`, `ott/operations.py` | Backup, restore, migration rehearsal, maintenance và release manifest. | Cần chạy theo runbook; manifest không thay thế bằng chứng nghiệm thu. |
| `deploy/cloudflare/` | Worker làm BFF, static assets, origin authentication và polling path. | Dùng `src/worker.js` với `wrangler.toml` theo runbook; không trộn config gateway cũ. |
| `judge/` | TCP/TLS protocol, tài khoản legacy, bài test tĩnh, compile và chấm điểm. | Runtime độc lập với `/api/v1` của OTT. |
| `core/` | Generic models, RBAC, validation, scoring, adapters và audit/rejudge. | Controller dùng dictionary trong RAM; tích hợp persistent/runtime còn là open issue. |

## 3. Luồng dữ liệu OTT

### Tạo giải và nộp bot

1. Browser gửi JSON tới `/api/v1` qua same-origin. Backend kiểm tra body, session, role và CSRF theo loại request.
2. Organizer tạo giải ở `DRAFT`, mở đăng ký và khởi động phase `CODING`.
3. Thí sinh lưu draft, yêu cầu compile check và nộp bản cuối. Bản cuối nhận receipt bền vững trước khi xử lý compile.
4. Store giữ source hash, trạng thái compile, quota/code state và quan hệ user-entry-tournament. Bản đã chọn bị đóng băng khi roster khóa.

### Chạy trận

1. Organizer khóa roster và yêu cầu tạo schedule.
2. Dispatcher claim trận; engine nạp snapshot, event history và clocks.
3. Runner khởi chạy bot process cô lập. Engine gửi một observation JSON line cho bên tới lượt; bot trả một action JSON line.
4. Engine kiểm tra action và áp dụng nước đi. Store ghi event, state và clock; dispatcher tiếp tục hoặc kết thúc trận.
5. API trả view đã lọc theo người xem. Trong phase placement, đối thủ không nhận placement bí mật. Khi phase play bắt đầu, board được reveal.

## 4. API HTTP `/api/v1`

Các nhóm route chính trong `ott/api.py`:

- Auth/session: `/auth/register`, `/auth/login`, `/auth/logout`, `/me`.
- Public metadata/operations: `/stats`, `/ops/status`, `/ops/maintenance`.
- Tournament lifecycle: `/tournaments`, `/tournaments/{tid}`, `open-registration`, `start-coding`, `lock`, `schedule`, `start`, `cancel`, `resume`.
- Entries và standings: `/tournaments/{tid}/entries`, `/entry`, `/standings`, `/entries/{eid}`.
- Bot workflow: `/bot-template`, `/tournaments/{tid}/code`, `/tournaments/{tid}/code/check`, `/tournaments/{tid}/code/submit`, `/bots/{bid}/source`.
- Match/watch: `/matches/{mid}`, `/live`, `/stream`, `/users/{name}`.

Danh sách này là nhóm API, không phải bản sao đầy đủ của OpenAPI. `docs/web-api.yaml` còn phản ánh contract thiết kế cũ và không khớp hoàn toàn với route/behavior hiện tại. Trước khi tích hợp client ngoài, đối chiếu `ott/api.py`, handler validation và các test API; cập nhật YAML riêng trong một thay đổi có chủ đích.

API trả lỗi JSON có `error.code` và message. Mutation cần CSRF sau khi xác thực session, ngoại trừ luồng auth được thiết kế riêng. Body tổng tối đa 1 MiB; source tối đa 256 KiB. Organizer mới được dùng route quản trị. Source endpoint chỉ dành cho chủ sở hữu bot.

## 5. Protocol bot OTT v1

- Transport: stdin/stdout của process bot; mỗi thông điệp là một dòng UTF-8 JSON kết thúc bằng LF.
- Mỗi dòng tối đa 1 MiB. `stdout` chỉ chứa action; chẩn đoán phải ghi vào `stderr`.
- Mỗi observation xác định `protocol_version`, `match_id`, `phase`, `side`, board được phép thấy, quân còn lại, clocks, legal actions và history được phép thấy.
- Action là một trong `place` hoặc `move`. Engine tự xác thực phase, tọa độ, lượt và luật. Không chấp nhận action thừa, dòng thứ hai hoặc JSON key trùng.
- Trong phase placement, observation không chứa vị trí đối phương. Sau reveal, process có thể được dựng lại từ snapshot/history được phép thấy; file trong container không phải storage bền vững.

Schema chuẩn nằm tại `docs/bot-protocol.schema.json`; game rules ở `docs/rules.md`. Kiểm tra contract code và schema cùng nhau nếu thay protocol.

## 6. Mô hình dữ liệu OTT

SQLite chứa các miền dữ liệu sau:

- `users`, `sessions`: danh tính, hash thông tin xác thực và phiên.
- `tournaments`: game/version, clock, trạng thái, người tạo và thời điểm.
- `entries`, `bot_submissions`: suất, draft, bot version, source hash, trạng thái compile và lựa chọn cuối.
- `submission_receipts`: bản nộp cuối bất biến, thời điểm nhận và trạng thái xử lý.
- `matches`, `match_events`: pairing, phase, state, clocks, event sequence và kết quả.
- `audit_log`, lease/attempt/resource records: audit và cơ chế điều phối/recovery được triển khai ở các phần hiện hành.

OTT store dùng migration có version; schema được xác định trong `ott/store.py`. `docs/database-schema.md` mô tả một phần schema mục tiêu cũ và cần đối chiếu với DDL/migration trước khi dùng làm migration spec. Không chạy migration trên database duy nhất; backup trước khi nâng cấp.

## 7. Các subsystem khác

### Judge TCP/TLS

`judge/server.py` phục vụ protocol JSON line qua TLS; `judge/client.py` là CLI. Server hỗ trợ register/login, list/show problem, submit, đọc kết quả và history. Bài được chấm theo test tĩnh trong Docker. SQLite và protocol này tách khỏi OTT HTTP API.

### Generic Core v2

`core/models.py`, `core/api_v2.py`, `core/adapters/` và `schemas/` chứa models, snapshot, RBAC, scoring và adapter cho bot/test suite. `CoreAPIv2Controller` hiện lưu definitions/seasons/entries/results bằng dictionary trong RAM. Adapter tests không chứng minh OTT dùng Core controller hoặc persistence dùng chung. Tích hợp CP/OTT trên một repository và dispatcher vẫn là cổng nghiệm thu riêng.

## 8. Tài liệu kỹ thuật nguồn

- [OpenAPI thiết kế](../web-api.yaml)
- [Core API v2](../core-api-v2.yaml)
- [Bot JSON Schema](../bot-protocol.schema.json)
- [Luật OTT](../rules.md)
- [Database schema](../database-schema.md)
- [Vòng đời trận](../match-lifecycle.md)
- [BFF-origin contract](../minh/bff-contract.md)

Tài liệu thiết kế không tự chứng minh code hoặc deployment khớp contract. Với behavior hiện tại, dùng source và test tương ứng làm bằng chứng.
