# Hệ thống chấm bài qua TCP

## Nền tảng thi đấu bot OTT (web + backend)

Gói `ott/` là backend Python (chỉ dùng thư viện chuẩn) gồm engine luật OTT v1, API HTTP `/api/v1`, scheduler vòng tròn hai lượt, runner bot tương tác và SQLite. Web client nằm ở [`mockup/`](mockup/README.md) và được cùng server phục vụ.

    python -m ott seed       # 1 organizer + 4 thí sinh; mật khẩu ở data/seed-accounts.json
    python -m ott serve --profile development --runner local  # chỉ phát triển ở http://127.0.0.1:8080

Demo Internet dùng Docker bắt buộc, source allowlist, gateway có origin key và SQLite schema 5. Đăng ký public hiện dùng tên đăng nhập và mật khẩu, không yêu cầu email. Hướng dẫn vận hành, backup/migration và cấu hình tại [`deploy/README.md`](deploy/README.md). Nộp source cuối trả HTTP 202 với receipt bền vững; hệ thống biên dịch trong hàng đợi.

Luồng: organizer tạo giải (đồng hồ ván 5/10/60 phút, thời gian viết bot mặc định 60 phút) → mở đăng ký → thí sinh đăng ký → organizer bấm **bắt đầu giai đoạn viết bot** → mỗi đội viết bot C++17, C17, Rust 2021 hoặc Go ngay trên web trong thời gian đếm ngược, tối đa **10 lượt biên dịch**, bản nháp tự lưu, **nộp một lần và không thể sửa**; hết giờ hệ thống tự nộp bản nháp đã lưu (nếu biên dịch được) → organizer khóa roster, sinh lịch, bắt đầu giải → bot tự đấu.

Yêu cầu: Python 3.11+, Linux Docker cho triển khai. Chế độ development local cần compiler tương ứng: `g++`, `gcc`, `rustc`, `go` hoặc JDK 17. Mã mẫu cho thí sinh: `samples/bot_template.*`.

Nhịp ván: sau mỗi nước đi hợp lệ engine dừng **2 giây** (`--move-delay-ms 2000`, mặc định) để người theo dõi kịp xem; khoảng dừng này không bị trừ vào đồng hồ bot. Đặt `--move-delay-ms 0` khi chạy test tự động hoặc chạy giải nhanh.

Kiểm thử: `python -m unittest tests.test_ott_engine -v` (luật, lịch, xếp hạng) và, khi server đang chạy, `python tests/e2e_api.py` (chơi trọn một giải 4 đội qua API; thêm `--auto-submit` để thử tự nộp khi hết giờ).

### Cập nhật trực tiếp trên LAN (SSE)

Server đẩy thông báo thay đổi qua `GET /api/v1/stream` (Server-Sent Events) ngay khi có nước đi, đổi trạng thái giải, đăng ký hoặc nộp bot; mọi máy trong LAN đang mở web nhận trong khoảng 0,1 giây thay vì chờ polling. Chân trang web hiện "● Trực tiếp" khi kết nối stream đang hoạt động; nếu mất stream, trình duyệt tự nối lại (1 giây) và các trang vẫn tự làm mới định kỳ.

- Sự kiện chỉ chứa id và bộ đếm (`type`, `id`, `tid`, `status`, `move_count`, `phase`), không có bàn cờ hay dữ liệu bí mật; trang ván đấu nhận sự kiện rồi tải lại `/matches/{id}`, nên quy tắc ẩn bố trí quân vẫn do API quyết định.
- Các sự kiện dồn nhau được gộp theo (loại, id) trong cửa sổ 80 ms; mỗi client giữ tối đa 256 sự kiện chờ. Khi đầy, stream gộp thành một sự kiện làm mới dữ liệu trang đang mở. Tối đa 300 kết nối stream (`stream_clients` trong `/api/v1/stats`).
- Server tiếp tục dùng một luồng cho mỗi kết nối, phù hợp lớp học nhỏ. API OTT chỉ bind loopback; dùng gateway riêng để chia sẻ, không mở trực tiếp cổng 8080 ra LAN hoặc Internet.
- Kiểm thử: `python -m unittest tests.test_ott_events -v`.

### Sandbox Docker cho bot

Runner mặc định `--runner docker`, profile `demo`; bắt buộc source allowlist và image compiler/runtime riêng. Chỉ dùng `--profile development --runner local` với mã nguồn tin cậy. Mỗi lần biên dịch và mỗi bot khi thi đấu chạy trong một container riêng, dùng xong xóa:

- không mạng (`--network=none`), root filesystem chỉ đọc, bỏ mọi capability, `no-new-privileges`, chạy user `65534` (không phải root);
- bot: 1 CPU, 256 MiB RAM (không swap), 64 PID, `/tmp` 16 MiB `noexec`, giới hạn file/descriptor; biên dịch: 1 CPU, 1 GiB RAM, 128 PID, timeout 15 giây (Go 30 giây);
- binary chỉ được mount read-only; container được `docker kill` và xóa khi ván kết thúc hoặc bot lỗi.

    python -m ott build-image --target compiler --image ott-bot-runner
    python -m ott build-image --target runtime --image ott-bot-runtime
    python -m ott serve --runner docker --runtime-image ott-bot-runtime --source-allowlist /etc/contest/source-allowlist.json
    python -m unittest tests.test_ott_sandbox -v    # test cô lập (tự bỏ qua khi không có Docker)

Thời gian khởi động container không tính vào đồng hồ bot (engine đợi container chạy rồi mới gửi observation đầu). Profile demo yêu cầu khôi phục artifact đã xác minh nếu thiếu; chỉ development local mới tự biên dịch lại. Server vẫn cần quyền dùng Docker daemon: chỉ chạy trên máy quản trị tin cậy (xem VibeSec).

**Bảo vệ API OTT.** API chỉ bind loopback; không mở cổng 8080 công khai. Dùng gateway riêng và làm theo [runbook triển khai](deploy/README.md). `--runner local` dành riêng cho development với mã tin cậy: bot chạy như process thường và có thể thực thi mã tùy ý bằng compiler được hỗ trợ trên host.

**Giới hạn hiện tại.** Chưa có lease/fencing epoch cho nhiều runner; recovery sau restart dùng snapshot cuối. Upload bot dùng JSON (`{"source": "..."}`) thay vì multipart, và API viết bot khác `docs/web-api.yaml` (xem `/tournaments/{id}/code`, `/code/check`, `/code/submit`). Docker runner chưa chạy trong container của chính server (server cần gọi `docker` trên host).

Tài liệu thiết kế OTT: [PRD](docs/PRD.md), [architecture](docs/architecture.md), [UI/UX Draft](docs/UI_UX_Draft.md), [rules](docs/rules.md), [database schema](docs/database-schema.md), [VibeSec](docs/VibeSec.md), [OpenAPI](docs/web-api.yaml), [bot protocol schema](docs/bot-protocol.schema.json), [match lifecycle](docs/match-lifecycle.md).

MVP gồm server Python, client dòng lệnh, protocol JSON trên TCP/TLS, SQLite và worker Docker cho bài C++, C, Rust và Go. Server được thiết kế để chạy trên một máy Linux trong LAN. Mã nguồn bài nộp chạy trong container mới cho từng test.

## Yêu cầu

- Python 3.11 trở lên
- Docker Engine trên Linux
- OpenSSL để tạo chứng chỉ TLS tự ký khi thử trong LAN

## Cài đặt

Tạo image chạy bài nộp từ thư mục dự án:

    docker build -f judge/Dockerfile.runner --target compiler -t lan-judge-cpp17 .
    docker build -f judge/Dockerfile.runner --target runtime -t lan-judge-runtime .

Tạo chứng chỉ dùng thử. Thay 192.168.1.10 bằng địa chỉ LAN của server:

    mkdir -p data
    openssl req -x509 -newkey rsa:3072 -nodes -days 365 \
      -keyout data/server.key -out data/server.crt \
      -subj "/CN=192.168.1.10" \
      -addext "subjectAltName=IP:192.168.1.10"

Giữ file data/server.key riêng tư. Phân phối data/server.crt cho client để xác minh server.

## Chạy server

    python -m judge.server \
      --host 0.0.0.0 \
      --port 8765 \
      --tls-cert data/server.crt \
      --tls-key data/server.key

Server yêu cầu Docker daemon hoạt động và hai image `lan-judge-cpp17` và `lan-judge-runtime` có sẵn. Mở cổng TCP 8765 trong firewall LAN. Dùng tài khoản hệ điều hành không phải root để chạy server.

## Dùng client

Các lệnh dưới đây dùng chứng chỉ server đã tạo ở trên. Thêm --host khi server không chạy trên cùng máy.

    python -m judge.client --host 192.168.1.10 --ca-cert data/server.crt problems
    python -m judge.client --host 192.168.1.10 --ca-cert data/server.crt show sum-two-integers
    python -m judge.client --host 192.168.1.10 --ca-cert data/server.crt register --username linh
    python -m judge.client --host 192.168.1.10 --ca-cert data/server.crt login --username linh
    python -m judge.client --host 192.168.1.10 --ca-cert data/server.crt submit sum-two-integers main.cpp --username linh
    python -m judge.client --host 192.168.1.10 --ca-cert data/server.crt result SUBMISSION_ID --username linh --follow
    python -m judge.client --host 192.168.1.10 --ca-cert data/server.crt history --username linh

Client hỏi mật khẩu qua prompt ẩn. Mỗi lệnh cần xác thực sẽ đăng nhập rồi gửi thao tác trên cùng kết nối TLS. Server chỉ lưu hash token; token hết hạn sau 12 giờ và client không ghi token ra đĩa. Tên tài khoản dài 3–32 ký tự ASCII gồm chữ, số, dấu gạch dưới. Mật khẩu dài 10–128 byte UTF-8.

## Protocol v1

Mỗi request và response là một JSON object UTF-8, kết thúc bằng LF. Một kết nối TLS có thể mang nhiều cặp request-response tuần tự. Server từ chối request line lớn hơn 1 MiB.

Request:

    {"version":1,"request_id":"id-1","op":"LIST_PROBLEMS","token":null,"data":{}}

Response thành công:

    {"request_id":"id-1","ok":true,"data":{"problems":[]}}

Response lỗi:

    {"request_id":"id-1","ok":false,"error":{"code":"AUTH_REQUIRED","message":"Log in before using this operation."}}

Operation: REGISTER, LOGIN, LIST_PROBLEMS, GET_PROBLEM, SUBMIT, GET_SUBMISSION, LIST_MY_SUBMISSIONS. Request có thể bỏ token với operation công khai và data rỗng. LOGIN trả token chỉ có hiệu lực trên kết nối hiện tại. `SUBMIT` nhận `cpp17`, `c17`, `rust2021`, `go` hoặc `java17`. Source tối đa 256 KiB.

## Thêm bài mẫu

Thêm một file JSON vào thư mục problems. Mỗi manifest khai báo id, title, statement, sample input/output và danh sách tests. Mỗi test có input, output, points; tổng điểm phải bằng 100. Có thể đặt time_limit_ms, memory_limit_mb, compile_timeout_sec; mặc định lần lượt là 2000, 256 và 15.

Test input và output bí mật không được gửi cho client. Server so sánh output theo token, nên khác biệt khoảng trắng không ảnh hưởng kết quả.

## Sandbox

Mỗi lần biên dịch và mỗi test chạy trong container riêng. Container không có mạng, dùng user không phải root, root filesystem chỉ đọc, bỏ Linux capabilities, cấm privilege escalation, giới hạn CPU/bộ nhớ/PID/output và thời gian. Server cần quyền truy cập Docker daemon; chỉ chạy server trên máy quản trị tin cậy.

## Kiểm thử

Chạy unit và protocol integration tests:

    python -m unittest discover -s tests -v

Docker integration tests tự bỏ qua khi Docker daemon hoặc image không có. Chạy toàn bộ verdict và kiểm tra chặn mạng khi image đã được build:

    python -m unittest tests.test_docker_integration -v


## Ngôn ngữ và bảo vệ nhiều lớp

Hỗ trợ `cpp17`, `c17`, `rust2021`, `go`, `java17` trên trình soạn thảo OTT và CLI judge (`submit --language`). Xem [cấu hình compiler, sandbox và firewall](docs/languages-security.md) trước khi nâng cấp máy triển khai.
