# VibeSec: Threat model cho cuộc thi bot OTT

## Trạng thái

Đánh giá hiện trạng và yêu cầu sản phẩm đích. Game engine, bot runner tương tác, tournament UI và schema OTT chưa được triển khai. Container hạn chế rủi ro nhưng không phải ranh giới bảo mật tuyệt đối.

## Tài sản và ranh giới tin cậy

Tài sản:

- Khóa TLS, hash mật khẩu, session và role organizer.
- Source bot, binary compile, bí mật xếp quân, lịch sử trận và kết quả.
- Database, queue, máy Linux host và Docker daemon.

```text
Thí sinh ── HTTPS ── Web gateway ── dịch vụ giải ── SQLite
                                      │
                                      └── Engine ── stdin/stdout ── Bot containers
Ban tổ chức ── cùng web gateway, quyền ORGANIZER
```

Source bot, action, observation và dữ liệu người dùng không tin cậy. Docker daemon, host, TLS key và dịch vụ lưu bí mật thuộc vùng tin cậy cao. Bot không giao tiếp trực tiếp với bot đối thủ hoặc mạng.

## Kiểm soát hiện có trong runner

- Container chạy non-root; image mặc định dùng UID/GID `65534`.
- Tắt mạng bằng `--network=none`; root filesystem chỉ đọc; bỏ capabilities; bật `no-new-privileges`.
- Runner hiện tại giới hạn runtime 1 CPU, RAM theo bài (mặc định 256 MiB), swap cùng mức RAM, PID 64 và timeout mỗi test.
- Compile hiện tại dùng 1 CPU, 1.024 MiB RAM, timeout mặc định 15 giây.
- `/tmp` là tmpfs có giới hạn; file tối đa 64 MiB; stdout/stderr được đọc có giới hạn; binary tối đa 64 MiB.
- Server TCP hiện dùng TLS tối thiểu 1.2; mật khẩu hash bằng `scrypt`; SQLite chỉ giữ SHA-256 của token.

Các cờ sandbox này là kiểm soát hiện có cho runner bài tĩnh. Bot runner OTT phải áp dụng cùng chính sách tài nguyên đã chọn và hỗ trợ stdin/stdout tương tác; hiện code chưa làm việc đó.

## Kiểm soát hiện có trong server legacy

- TCP server mặc định bind `0.0.0.0`, cổng `8765`; TLS tối thiểu 1.2. CLI kiểm tra CA và hostname.
- Request line giới hạn 1 MiB; credential hash bằng `scrypt`; session token ngẫu nhiên, DB chỉ lưu SHA-256, TTL 12 giờ.
- Token chỉ hợp lệ trên kết nối TLS đã login. Legacy chưa có logout/revoke operation.
- Query bài nộp lọc theo cả submission ID và user ID. SQL dùng bind parameters.
- Mỗi process server tạo hai worker; giới hạn này không áp dụng toàn cụm.
- Legacy chưa có rate limit, quota queue, giới hạn số kết nối hoặc read deadline. Client có thể giữ kết nối bằng cách gửi chậm.
- SQLite bật WAL và `busy_timeout`; ứng dụng chưa đặt ACL/file mode hoặc mã hóa at-rest cho DB, WAL, SHM và backup.

Các điểm trên mô tả `judge/server.py`, `judge/client.py`, `judge/database.py` và `judge/judge_engine.py`; không phải kiểm soát của thiết kế OTT.

## Mối đe dọa và yêu cầu đích

| Mối đe dọa | Kiểm soát bắt buộc |
|---|---|
| Bot đọc file host hoặc truy cập mạng | Chạy mỗi bot trong container riêng, non-root, không mạng, root filesystem chỉ đọc, bỏ capabilities, `no-new-privileges`; chỉ mount source/binary cần thiết |
| Bot chiếm CPU/RAM/PID | Mỗi bot: 1 CPU, 256 MiB RAM, 64 PID; mỗi ván dùng đồng hồ tổng đã chọn; output/action bị giới hạn |
| Bot chiếm compiler | Compile trước khi khóa giải; 1 CPU, 1.024 MiB RAM, timeout 15 giây; giới hạn output compiler và binary |
| Bot giả action hoặc làm hỏng trạng thái | Engine tự tính nước hợp lệ, xác thực action và chỉ ghi state qua transaction |
| Lộ bố trí bí mật | Trong phase đặt, chỉ bot đang đi nhận placement của mình; API không trả raw `match_events`/`state_json`; reveal toàn bàn khi vào phase chơi |
| Thí sinh hoặc organizer đọc source người khác | Source endpoint chỉ cho chủ bot; organizer chỉ xem roster, bot version và compile status |
| Giả mạo hoặc lạm dụng organizer | Role chỉ do người vận hành cấp; kiểm tra quyền ở mọi thao tác quản lý giải; ghi audit actor và thời gian |
| Bot khai thác protocol | JSON UTF-8, tối đa 1 MiB mỗi dòng, từ chối key trùng và output thừa; timeout theo clock; engine parse schema rồi tự xác thực action |
| Mất tiến trình khi restart | Ghi action, state và đồng hồ sau mỗi lượt; dừng clock khi server offline; khởi động lại bot từ log đã xác thực |
| Chiếm host qua Docker daemon | Chỉ cấp quyền socket cho tài khoản vận hành tin cậy; firewall LAN; cập nhật host kernel và Docker |

## Bot process và clock

- Mỗi bên chạy process bot riêng suốt một trận. Process chỉ dùng stdin/stdout với engine; không mở listener hoặc gọi network.
- Engine giới hạn mỗi dòng 1 MiB, kiểm tra action và hủy container nếu bot timeout hoặc vượt resource cap.
- Đồng hồ chỉ chạy cho bên đang hành động; tính cả placement. Compile diễn ra trước trận.
- Lỗi bot như crash, JSON lỗi, action sai hoặc hết giờ làm bot thua. Lỗi hạ tầng làm trận `PAUSED`; không phạt bot.
- Sau restart, process mới nhận snapshot và history được phép thấy. File ghi trong container là tạm; bot dựng lại state từ history. Trong phase placement, không gửi placement đối thủ. Khi vào phase play, hai bot nhận toàn bàn.
- Ghi event, snapshot, clocks và bên đi trong cùng transaction. Dùng runner lease có fencing epoch để ngăn hai runner commit cùng một trận. Checkpoint clock mỗi giây; downtime không bị trừ, sai số tối đa một giây.
- Không ghi token, mật khẩu hoặc source vào log ứng dụng. Chỉ lưu compiler/runtime message đã giới hạn và lọc thông tin host.
- Xóa source, binary, state chi tiết và event log sau 90 ngày từ lúc giải kết thúc; giữ kết quả tổng hợp. Job purge bỏ qua giải chưa hoàn tất.

## Web và tài khoản organizer

- **Thiết kế đích:** HTTPS same-origin reverse proxy, TLS tối thiểu 1.2. Cookie phiên `Secure`, `HttpOnly`, `SameSite=Lax`; mutation có CSRF token. Same-origin không cần CORS allowlist.
- **Thiết kế đích:** body limit, deadline request, rate limit đăng nhập/upload, quota queue và giới hạn kết nối phải cấu hình được. Giá trị cụ thể là deployment parameters; chưa có trong code.
- **Thiết kế đích:** role organizer chỉ do operator cấp. Organizer có thể tạo/khóa/hủy giải và xem trạng thái, nhưng không xem source hoặc placement bí mật trước reveal.
- **Thiết kế đích:** audit log lưu actor, operation, tournament ID và thời điểm; không lưu secret/source. Phải giới hạn ai đọc audit và đặt retention.
- **Thiết kế đích:** DB, WAL/SHM, binary cache và backup nằm ngoài vùng public; quyền file tối thiểu. Phương thức mã hóa at-rest và ACL host cần được chốt trong cấu hình triển khai; code hiện tại chưa cung cấp.
- Session web có logout/revoke; đây là API thiết kế, không có trong TCP server legacy.

## Giới hạn còn lại

- Docker dùng chung kernel host; container escape vẫn có thể gây ảnh hưởng host.
- Docker socket thường tương đương quyền root. Không chạy server trên máy chứa dữ liệu nhạy cảm khác.
- Người có quyền root trên host hoặc Docker daemon có thể đọc memory, database và placement. Hạn chế quyền operator, ghi audit truy cập host, không coi Docker là VM.
- Cần kiểm thử sandbox trên Linux/Docker đích trước khi nhận bot thật. Đặc biệt kiểm tra mạng bị chặn, memory/PID/time/output limits và khôi phục clock.
- Rate limit, quota queue, deadline đọc request, ACL storage, at-rest encryption, audit log và backup OTT chưa có trong code hiện tại.

Kiến trúc và luồng dữ liệu: [architecture](architecture.md). API web: [OpenAPI](web-api.yaml). Bot contract: [JSON Schema](bot-protocol.schema.json). Recovery: [match lifecycle](match-lifecycle.md). Tổng quan sản phẩm: [PRD](PRD.md). Luồng UI: [UI/UX Draft](UI_UX_Draft.md). Luật: [rules](rules.md). Dữ liệu: [database schema](database-schema.md).
