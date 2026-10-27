# Đặc tả yêu cầu hệ thống

Phiên bản: 1.0

Đối tượng đọc: chủ dự án, ban tổ chức, người phát triển và người vận hành.

## 1. Mục tiêu

LapTrinhThiDau cung cấp nền tảng thi đấu bot OTT Arena. Thí sinh đăng ký giải, viết và nộp bot, theo dõi lịch cùng kết quả. Ban tổ chức cấu hình giải, khóa danh sách thi đấu và theo dõi trận. Engine áp dụng luật thống nhất; bot chạy trong sandbox.

Repo còn chứa judge TCP/TLS cho bài lập trình theo test tĩnh và một prototype Generic Core v2. Hai phần này có phạm vi vận hành riêng; chưa có tích hợp bền vững end-to-end với OTT Arena.

## 2. Trạng thái và cách diễn giải

OTT Arena có mã nguồn cho web client, HTTP API, luật OTT, scheduler, SQLite, live events và Docker runner. `mockup/README.md` xác nhận web client gọi API thật; tên thư mục `mockup` không phản ánh trạng thái hiện tại.

Mã nguồn hiện có không tự chứng minh release đã đạt. Hồ sơ `docs/release-acceptance.md` còn các cổng triển khai, cô lập compiler, tích hợp Core và nghiệm thu staging chưa hoàn tất. Các yêu cầu dưới đây ghi “Có mã nguồn” khi có implementation; trạng thái nghiệm thu được ghi riêng.

## 3. Người dùng và quyền

- **Thí sinh (`CONTESTANT`)**: tạo tài khoản, tham gia tối đa một suất trong mỗi giải, viết bot, xem lịch, bảng điểm và kết quả. Chỉ chủ tài khoản được đọc source bot của mình.
- **Ban tổ chức (`ORGANIZER`)**: tạo và cấu hình giải, điều khiển trạng thái giải, xem trạng thái compile và kết quả. Quyền organizer do người vận hành cấp; người dùng không tự nâng quyền.
- **Người vận hành**: quản lý máy chủ, cấu hình secret, Docker, firewall, backup, migration và cấp vai trò.
- **Người xem**: xem thông tin công khai và trận theo quyền API; không được xem source hoặc placement bí mật trước reveal.

## 4. Luồng nghiệp vụ chính

1. Organizer tạo giải, đặt tên, chọn đồng hồ mỗi bên là 300, 600 hoặc 3.600 giây và đặt thời gian viết bot. Mặc định thời gian viết là 60 phút.
2. Organizer mở đăng ký. Thí sinh đăng ký một suất; cần ít nhất hai suất để mở giai đoạn viết bot.
3. Organizer bắt đầu giai đoạn viết bot. Thí sinh soạn và lưu nháp trên web, biên dịch kiểm tra với giới hạn 10 lượt, rồi nộp bản cuối một lần. Hết hạn, hệ thống tự nộp nháp đã lưu nếu bản đó biên dịch được.
4. Organizer khóa danh sách khi các suất đủ điều kiện. Hệ thống tạo lịch vòng tròn hai lượt và khởi chạy trận.
5. Bot chơi qua stdin/stdout với engine. API lưu action, trạng thái và clock; web cập nhật lịch, ván và bảng xếp hạng.

Trạng thái giải: `DRAFT`, `REGISTRATION_OPEN`, `CODING`, `LOCKED`, `RUNNING`, `FINISHED`, `CANCELLED`.

## 5. Yêu cầu chức năng

| ID | Yêu cầu | Trạng thái trong repo |
|---|---|---|
| FR-01 | Tạo tài khoản, đăng nhập/đăng xuất, phiên cookie và phân quyền thí sinh/organizer. | Có trong OTT API; cấu hình đăng ký công khai phụ thuộc profile. |
| FR-02 | Tạo giải, mở/đóng các giai đoạn, đăng ký suất và khóa roster. | Có trong OTT API và SQLite. |
| FR-03 | Soạn nháp bot, lưu tự động, biên dịch kiểm tra và nộp bản cuối bất biến. | Có trong web/API; bản nộp cuối tạo receipt bền vững và được xử lý qua hàng đợi. |
| FR-04 | Hỗ trợ C++17, C17, Rust 2021, Go và Java 17; giới hạn source tối đa 256 KiB. | Có trong cấu hình runner hiện tại. |
| FR-05 | Sinh lịch vòng tròn hai lượt, đổi màu giữa hai ván, hỗ trợ bye. | Có trong scheduler OTT. |
| FR-06 | Chơi OTT v1 trên bàn 9×9, kiểm tra action và tính thắng/hòa/thua. | Có trong engine OTT. |
| FR-07 | Lưu lịch sử trận, clock, kết quả, standings và dữ liệu cần cho phục hồi. | Có trong store/dispatcher; bot process đang chạy không được khôi phục RAM giữa trận. |
| FR-08 | Cung cấp web client, giao diện organizer, xem trận và cập nhật trạng thái. | Có trong `mockup/`; SSE dùng cho mạng nội bộ, profile demo/tunnel dùng polling. |
| FR-09 | Chạy bài nộp trong môi trường cách ly, giới hạn tài nguyên và kiểm soát artifact. | Có sandbox Docker; profile demo bắt buộc Docker và source allowlist. Nghiệm thu trên Linux đích còn mở. |
| FR-10 | Hỗ trợ bài chấm test tĩnh qua dịch vụ TCP/TLS. | Có trong `judge/`; chạy như hệ thống legacy, độc lập với luồng giải OTT. |
| FR-11 | Cung cấp Core API v2 dùng adapter cho nhiều kiểu cuộc thi. | Có prototype trong `core/`; controller giữ dữ liệu trong bộ nhớ và chưa dùng chung persistent store/dispatcher của OTT. |

## 6. Luật OTT v1

- Bàn cờ 9×9, cột `a`–`i`, hàng `1`–`9`. Mỗi bên có 3 Kéo, 3 Búa, 3 Bao và 1 Vua.
- Hai bên luân phiên đặt quân vào hai hàng nhà của mình. Vị trí đặt của đối thủ được giữ kín đến khi phase chơi bắt đầu.
- Quân thường đi một ô tiến, chéo tiến hoặc chéo lùi; chỉ bắt theo hướng tiến. Kéo bắt Bao, Bao bắt Búa, Búa bắt Kéo. Quân thường cùng loại không bắt nhau.
- Quân thường tới hàng cuối phải phong cấp Xe hoặc Tượng. Quân phong cấp giữ loại Kéo/Búa/Bao và có hướng đi bổ sung theo luật OTT.
- Vua đi một ô theo tám hướng và không bắt. Quân địch bắt được Vua nếu nước bắt hợp lệ.
- Bắt Vua hoặc loại hết quân đối phương thì thắng. Hết nước hợp lệ, hết clock hoặc bot vi phạm protocol thì thua. Lặp trạng thái ba lần hoặc đạt 500 nước đi hợp lệ thì hòa.
- Mỗi cặp thí sinh đấu hai ván và đổi màu. Thắng được 3 điểm, hòa 1, thua 0. Tie-break: điểm tổng, điểm đối đầu trong nhóm hòa, hiệu số thắng-thua; nếu vẫn bằng thì đồng hạng.

Chi tiết luật và protocol chính thức trong repo: [rules.md](../rules.md) và [bot-protocol.schema.json](../bot-protocol.schema.json).

## 7. Yêu cầu phi chức năng

- Python 3.11 trở lên. OTT API bind loopback; web truy cập API cùng origin. Không mở API OTT trực tiếp ra Internet.
- Browser dùng cookie `Secure`, `HttpOnly`, `SameSite=Lax`; mutation có CSRF token. Public gateway xác thực origin bằng secret và giới hạn body.
- Source/binary bot không công khai. Trong phase placement, API không tiết lộ placement của đối thủ. Engine là nguồn xác thực action và trạng thái trận.
- Mỗi bot chạy trong container riêng, không mạng, non-root, root filesystem chỉ đọc, với CPU/RAM/PID/time/output limits.
- SQLite là kho MVP. Artifact và dữ liệu trận chi tiết cần retention/purge theo hồ sơ vận hành; kiểm soát ACL, mã hóa at-rest và backup production cần được xác nhận khi triển khai.
- Hiệu năng, tải và độ sẵn sàng chưa có bằng chứng release hoàn chỉnh; xem checklist nghiệm thu.

## 8. Ngoài phạm vi hiện tại

- Tích hợp hoàn chỉnh Generic Core v2 với store, scheduler và dispatcher bền vững của OTT.
- Khôi phục nguyên trạng RAM của bot process đang chạy sau khi máy chủ khởi động lại.
- Mở API trực tiếp ra Internet hoặc nhận source tùy ý từ Internet trong demo.
- Bảo đảm an toàn tuyệt đối của container dùng chung kernel.
- Playoff, game khác OTT v1, đội nhiều thành viên và chỉnh bot sau khi roster bị khóa.

## 9. Tiêu chí chấp nhận

- Người dùng hoàn tất luồng đăng ký, viết bot, nộp bản cuối và xem kết quả với đúng phạm vi quyền.
- Organizer tạo giải, khóa roster và sinh lịch hai lượt với màu, bye, điểm và tie-break đúng.
- Engine xử lý placement ẩn, nước đi, phong cấp, bắt Vua, timeout, action sai, hòa và lỗi runner theo luật.
- Restart không trừ clock trong thời gian server dừng; lỗi hạ tầng không bị tính thành lỗi bot.
- Linux staging xác minh Docker isolation, gateway, backup/restore, migration, recovery và tải đại diện. Mỗi kết quả phải có commit, cấu hình đã lọc secret, image ID và bằng chứng.

Các tiêu chí triển khai chưa đạt được đánh dấu tại [release-acceptance.md](../release-acceptance.md); không coi tài liệu này là biên bản nghiệm thu.
