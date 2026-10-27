# Database schema: SQLite

## Phạm vi

Phần **Legacy** mô tả schema do `judge/database.py` tạo. Phần **OTT hiện tại** mô tả database giải bot, `PRAGMA user_version=5`. SQLite tiếp tục là database MVP.

## Migration đích

- Đọc `PRAGMA user_version` lúc khởi tạo. Áp dụng migration tuần tự trong transaction; cập nhật version sau khi migration thành công.
- Schema 5 thêm `users.email`, `users.email_verified_at` và bảng token xác minh. Tài khoản cũ giữ `email=NULL`; đăng nhập username tiếp tục hoạt động.
- Sao lưu database/WAL trước migration và kiểm tra foreign key sau migration.

## Legacy hiện có

### `users`

| Cột | Kiểu | Ràng buộc | Ý nghĩa |
|---|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | ID user |
| `username` | TEXT | NOT NULL, UNIQUE | Username chuẩn hóa chữ thường |
| `password_hash` | TEXT | NOT NULL | Hash `scrypt` |
| `created_at` | INTEGER | NOT NULL | Unix epoch giây |

### `sessions`

| Cột | Kiểu | Ràng buộc | Ý nghĩa |
|---|---|---|---|
| `token_hash` | TEXT | PRIMARY KEY | SHA-256 token dạng hex |
| `user_id` | INTEGER | NOT NULL, FK `users.id` ON DELETE CASCADE | Chủ session |
| `expires_at` | INTEGER | NOT NULL | Hạn session, Unix epoch giây |

Chỉ mục `sessions_expiry_idx(expires_at)`.

### `submissions`

| Cột | Kiểu | Ràng buộc | Ý nghĩa |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | UUID bài nộp |
| `user_id` | INTEGER | NOT NULL, FK `users.id` | Chủ bài |
| `problem_id` | TEXT | NOT NULL | ID bài tĩnh |
| `language` | TEXT | NOT NULL | `cpp17`, `c17`, `rust2021`, `go` hoặc `java17` |
| `source` | TEXT | NOT NULL | Source bài tĩnh |
| `status` | TEXT | NOT NULL | `QUEUED`, `RUNNING`, `FINISHED`, `SYSTEM_ERROR` |
| `verdict` | TEXT | nullable | Verdict chấm test |
| `score` | INTEGER | NOT NULL DEFAULT 0 | Điểm bài tĩnh |
| `test_results_json` | TEXT | NOT NULL DEFAULT `[]` | Kết quả test |
| `message` | TEXT | nullable | Thông báo lỗi |
| `created_at` | INTEGER | NOT NULL | Unix epoch giây |
| `updated_at` | INTEGER | NOT NULL | Unix epoch giây |

Chỉ mục `submissions_user_created_idx(user_id, created_at DESC)` và `submissions_queue_idx(status, created_at)`. Khi khởi tạo database, `RUNNING` trở lại `QUEUED`. Database bật foreign keys, `busy_timeout=10000` và WAL.

Hai index trên có trong DDL hiện tại. Query claim xếp theo `rowid`; query history cũng xếp `rowid DESC`, nên index queue không xác định thứ tự FIFO hiện tại và index history không khớp thứ tự thực thi.

## OTT hiện tại (`user_version=5`)

### Mở rộng `users`

Các cột OTT gồm `role TEXT NOT NULL DEFAULT 'CONTESTANT'`, `email TEXT` nullable và `email_verified_at INTEGER` nullable. Email có unique index không phân biệt hoa thường. Giá trị role: `CONTESTANT`, `ORGANIZER`; người vận hành cấp `ORGANIZER`, không cho đăng ký role tự do. Email chưa xác minh không thể đăng nhập.

### `email_verification_tokens`

Lưu SHA-256 token, user ID duy nhất và hạn 24 giờ. Xóa token sau xác minh và dọn token hết hạn lúc khởi động.

### `tournaments`

| Cột | Kiểu | Ràng buộc | Ý nghĩa |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | UUID giải |
| `name` | TEXT | NOT NULL | Tên giải |
| `game_id` | TEXT | NOT NULL | Hiện là `ott` |
| `game_version` | TEXT | NOT NULL | Hiện là `1` |
| `clock_seconds` | INTEGER | NOT NULL | `300`, `600` hoặc `3600` mỗi bên |
| `status` | TEXT | NOT NULL | `DRAFT`, `REGISTRATION_OPEN`, `LOCKED`, `RUNNING`, `FINISHED`, `CANCELLED` |
| `created_by` | INTEGER | NOT NULL, FK `users.id` | Organizer tạo giải |
| `created_at` | INTEGER | NOT NULL | Unix epoch giây |
| `started_at` | INTEGER | nullable | Thời điểm bắt đầu |
| `finished_at` | INTEGER | nullable | Thời điểm kết thúc |

Chỉ mục: `tournaments_status_idx(status, created_at)`.

### `bot_submissions`

Mỗi upload tạo phiên bản mới bất biến. Binary compile nằm ngoài SQLite trong thư mục riêng. Sau 90 ngày từ khi tournament kết thúc, source bị xóa; hash và metadata được giữ.

| Cột | Kiểu | Ràng buộc | Ý nghĩa |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | UUID phiên bản bot |
| `tournament_id` | TEXT | NOT NULL, FK `tournaments.id` | Game/version đích |
| `user_id` | INTEGER | NOT NULL, FK `users.id` | Người gửi |
| `language` | TEXT | NOT NULL | `cpp17`, `c17`, `rust2021`, `go` hoặc `java17` |
| `source` | TEXT | NULL sau retention | Source bot; xóa sau 90 ngày |
| `source_sha256` | TEXT | NOT NULL | Hash source |
| `compile_status` | TEXT | NOT NULL | `QUEUED`, `READY`, `CE` |
| `compile_message` | TEXT | nullable | Lỗi compile đã giới hạn độ dài |
| `source_purged_at` | INTEGER | nullable | Thời điểm source bị xóa |
| `created_at` | INTEGER | NOT NULL | Unix epoch giây |

Chỉ mục: `bot_submissions_entry_idx(tournament_id, user_id, created_at DESC)`.
Ràng buộc `UNIQUE(id, tournament_id, user_id)` hỗ trợ khóa ngoại ghép từ `entries`.

### `entries`

| Cột | Kiểu | Ràng buộc | Ý nghĩa |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | UUID suất dự thi |
| `tournament_id` | TEXT | NOT NULL, FK `tournaments.id` | Giải |
| `user_id` | INTEGER | NOT NULL, FK `users.id` | Thí sinh |
| `bot_submission_id` | TEXT | nullable, FK `bot_submissions.id` | Bot được chốt |
| `status` | TEXT | NOT NULL | `REGISTERED`, `READY`, `REMOVED` |
| `registered_at` | INTEGER | NOT NULL | Unix epoch giây |

Ràng buộc `UNIQUE(tournament_id, user_id)` bảo đảm một suất mỗi user mỗi giải; `UNIQUE(id, tournament_id)` hỗ trợ tham chiếu match cùng giải. Thí sinh chọn phiên bản cuối; organizer xác nhận bot hợp lệ trước khi giải chuyển `LOCKED`.

Ràng buộc khóa ngoại ghép `(bot_submission_id, tournament_id, user_id)` tham chiếu `(bot_submissions.id, tournament_id, user_id)`, bảo đảm bot thuộc đúng user và giải. `READY` chỉ hợp lệ khi đã chọn bot `READY`. Suất và bot đã chốt không đổi sau `LOCKED`; không hard-delete suất đã có trận.

### `matches`

| Cột | Kiểu | Ràng buộc | Ý nghĩa |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | UUID trận |
| `tournament_id` | TEXT | NOT NULL, FK `tournaments.id` | Giải |
| `round_no` | INTEGER | NOT NULL | Vòng |
| `leg_no` | INTEGER | NOT NULL | Lượt 1 hoặc 2 |
| `pairing_no` | INTEGER | NOT NULL | Cặp trong vòng |
| `white_entry_id` | TEXT | NOT NULL, FK `entries.id` | Suất cầm Trắng |
| `black_entry_id` | TEXT | NOT NULL, FK `entries.id` | Suất cầm Đen |
| `status` | TEXT | NOT NULL | `SCHEDULED`, `RUNNING`, `PAUSED`, `FINISHED` |
| `result` | TEXT | nullable | `WHITE_WIN`, `BLACK_WIN`, `DRAW` |
| `result_reason` | TEXT | nullable | Lý do kết thúc |
| `phase` | TEXT | NOT NULL | `PLACEMENT` hoặc `PLAY` |
| `side_to_act` | TEXT | nullable | `WHITE` hoặc `BLACK` |
| `state_json` | TEXT | nullable sau retention | Snapshot bàn và trạng thái engine |
| `white_clock_ms` | INTEGER | NOT NULL | Thời gian còn lại của Trắng |
| `black_clock_ms` | INTEGER | NOT NULL | Thời gian còn lại của Đen |
| `clock_checkpoint_at` | INTEGER | nullable | Unix epoch giây của clock checkpoint gần nhất |
| `move_count` | INTEGER | NOT NULL DEFAULT 0 | Action đi quân hợp lệ |
| `started_at` | INTEGER | nullable | Unix epoch giây |
| `finished_at` | INTEGER | nullable | Unix epoch giây |
| `updated_at` | INTEGER | NOT NULL | Unix epoch giây |
| `lease_epoch` | INTEGER | NOT NULL DEFAULT 0 | Fencing counter tăng khi runner claim lại |
| `lease_owner` | TEXT | nullable | ID runner đang giữ lease |
| `lease_expires_at` | INTEGER | nullable | Hết hạn lease, Unix epoch giây |
| `engine_version` | TEXT | NOT NULL | Phiên bản engine được pin cho trận |
| `state_version` | INTEGER | NOT NULL | Phiên bản snapshot state |

Ràng buộc `UNIQUE(tournament_id, leg_no, round_no, pairing_no)`, `CHECK(white_entry_id <> black_entry_id)`, khóa ngoại ghép `(white_entry_id, tournament_id)` và `(black_entry_id, tournament_id)` đến `entries(id, tournament_id)`. Chỉ mục `matches_queue_idx(status, tournament_id, leg_no, round_no)` và `matches_lease_idx(lease_expires_at)`.

`SCHEDULED` chưa có runner; `RUNNING` có `side_to_act` và lease; `PAUSED` không trừ clock; `FINISHED` có result/reason và không có runner. `state_json` bắt buộc trong trận chưa kết thúc, nhưng được xóa sau retention; vì vậy cột đích nullable cho trận `FINISHED` đã purge. Bye không tạo match row.

Bye không tạo trận và không được điểm; UI suy ra bye từ lịch vòng tròn. `white_entry_id` và `black_entry_id` phải tham chiếu suất cùng giải. Thứ hạng được tính từ kết quả `matches`; không cần bảng standings riêng.

### `match_events`

Append-only log dùng audit và khôi phục trong thời hạn lưu. Clock hiện hành được checkpoint ít nhất mỗi giây trong `matches`; event lưu clock sau action. Chỉ backend tin cậy đọc event đặt bí mật trước reveal.

| Cột | Kiểu | Ràng buộc | Ý nghĩa |
|---|---|---|---|
| `match_id` | TEXT | NOT NULL, FK `matches.id` ON DELETE CASCADE | Trận |
| `sequence_no` | INTEGER | NOT NULL | Số thứ tự event |
| `phase` | TEXT | NOT NULL | `PLACEMENT` hoặc `PLAY` |
| `side` | TEXT | NOT NULL | `WHITE` hoặc `BLACK` |
| `action_json` | TEXT | NOT NULL | Action được engine chấp nhận |
| `state_after_json` | TEXT | NOT NULL | Snapshot sau action |
| `white_clock_ms` | INTEGER | NOT NULL | Clock sau action |
| `black_clock_ms` | INTEGER | NOT NULL | Clock sau action |
| `created_at` | INTEGER | NOT NULL | Unix epoch giây |

Khóa chính tổng hợp: `(match_id, sequence_no)`. Sau mỗi action, transaction lưu event và cập nhật snapshot/clock của `matches`. Khi restart, bot được chạy lại với snapshot và lịch sử mà bot có quyền thấy.

Mỗi action commit cập nhật event, snapshot, phase, bên đi và clock trong cùng transaction; runner phải kèm `lease_epoch` hiện hành. Recovery nạp snapshot đã commit cuối, replay event mới hơn nếu cần, rồi tạo history projection theo quyền bot. API không trả trực tiếp bảng event.

## Retention và trạng thái sau purge

- Tính hạn 90 ngày từ `tournaments.finished_at`.
- Khi hết hạn, xóa `bot_submissions.source`, binary artifact, `matches.state_json` và các dòng `match_events`; đặt `source_purged_at` và metadata purge tương ứng.
- Giữ bot ID/hash, user/tournament/entry, kết quả trận, reason, điểm và standings có thể tính lại từ match result.
- Không purge dữ liệu của tournament chưa `FINISHED` hoặc đang cần recovery.

Vòng đời đầy đủ xem tại [match lifecycle](match-lifecycle.md). API projection xem tại [OpenAPI](web-api.yaml).

Rủi ro bảo vệ source và log xem tại [VibeSec](VibeSec.md). Quy tắc dữ liệu xem tại [rules](rules.md). Kiến trúc xem tại [architecture](architecture.md).
