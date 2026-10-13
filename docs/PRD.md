# PRD: Hệ thống thi đấu bot OTT

## Trạng thái tài liệu

- **Hiện có trong code:** judge C++17 theo bộ test tĩnh, server TCP/TLS, CLI, SQLite và Docker runner.
- **Sản phẩm đích:** nền tảng thi đấu bot OTT v1, có giao diện web, game engine, lịch giải và bot runner tương tác.
- **Mockup có trong repo:** frontend tĩnh dùng dữ liệu mẫu, không gọi API và không lưu dữ liệu.
- **Trạng thái OTT:** gateway, API, game engine, tournament service và bot runner OTT vẫn là thiết kế; chưa có trong code.

Judge theo test tĩnh được giữ làm hệ thống legacy. PRD này chuyển trọng tâm sản phẩm sang thi đấu bot.

## Bối cảnh và mục tiêu

Thí sinh viết bot để tự chơi game do ban tổ chức phát hành. Game đầu tiên là OTT trên bàn 9×9. Nền tảng biên dịch bot, ghép trận vòng tròn, chạy hai bot trong sandbox và lưu kết quả.

Mục tiêu:

1. Thí sinh đăng ký giải, gửi bot C++17 và xem lịch, điểm, kết quả.
2. Ban tổ chức tạo giải OTT, chọn đồng hồ, khóa danh sách bot và khởi chạy lịch thi.
3. Engine áp dụng cùng một bộ luật cho mọi bot, kiểm tra nước đi và ghi trạng thái trận.
4. Hệ thống cân bằng màu bằng hai lượt đấu, lưu trạng thái để khôi phục trận sau khi server khởi động lại.
5. Sandbox hạn chế tác động của mã do thí sinh gửi.

## Người dùng và quyền

- **Thí sinh:** một tài khoản có tối đa một suất trong mỗi giải; có thể thay bot trước khi khóa giải.
- **Ban tổ chức:** tạo và quản lý giải bằng tài khoản có vai trò `ORGANIZER`.
- **Người vận hành:** cấp vai trò organizer, triển khai server, Docker, TLS và dữ liệu game.

Người vận hành cấp quyền organizer ngoài luồng đăng ký công khai. Tài khoản thí sinh không tự nâng quyền.

- Organizer quản lý giải và xem compile status; chỉ chủ tài khoản xem source bot.
- Organizer và người xem không thấy placement trước reveal. Engine giữ full state.

## Luồng sản phẩm

### Thí sinh

1. Đăng ký hoặc đăng nhập.
2. Xem giải OTT đang mở và đăng ký một suất.
3. Gửi source C++17. Server biên dịch để kiểm tra; bot lỗi biên dịch chưa đủ điều kiện dự giải.
4. Thay bot nếu cần trước khi giải chuyển sang `LOCKED`.
5. Xem lịch đấu, màu quân, trạng thái trận, bảng điểm và kết quả.

### Ban tổ chức

1. Tạo giải OTT v1 và chọn đồng hồ 5 phút, 10 phút hoặc 1 giờ cho mỗi bên.
2. Mở đăng ký, xem các suất và trạng thái biên dịch.
3. Khóa đăng ký. Xác nhận mỗi suất đã chọn bot hợp lệ; phiên bản bot không đổi trong giải.
4. Tạo lịch vòng tròn hai lượt và khởi chạy hàng đợi trận.
5. Theo dõi trạng thái giải, kết quả và bảng xếp hạng.

### Bot runner

- Biên dịch mỗi phiên bản bot bằng C++17 trước khi tính giờ trận.
- Chạy một process bot cho mỗi bên trong suốt trận. Engine gửi một observation JSON line và nhận một action JSON line mỗi lượt.
- Chỉ engine thay đổi bàn cờ. Engine xác thực action theo danh sách nước hợp lệ.
- Ghi log sau mỗi action được chấp nhận, gồm trạng thái và đồng hồ. Khi khởi động lại, chạy bot process mới từ snapshot và history được phép thấy.
- File ghi trong container là tạm; bot không dựa vào file để khôi phục trạng thái.

Protocol chi tiết nằm trong [rules](rules.md). Giao diện người dùng nằm trong [UI/UX Draft](UI_UX_Draft.md).

## OTT v1

- Bàn 9×9; mỗi bên có 3 Kéo, 3 Búa, 3 Bao và 1 Vua.
- Hai bên luân phiên đặt từng quân trong hai hàng nhà mình. Đối thủ không thấy lượt đặt; toàn bộ bố trí được lộ khi bắt đầu đi quân.
- Quân thường đi một ô tiến, chéo tiến hoặc chéo lùi; chỉ bắt quân tiến hoặc chéo tiến.
- Kéo bắt Bao, Bao bắt Búa, Búa bắt Kéo. Quân thường cùng loại không bắt nhau.
- Quân thường tới hàng cuối được chọn phong cấp Xe hoặc Tượng. Quân phong cấp giữ loại, đi theo hướng đã chọn và thêm một ô theo 8 hướng; quân trượt không nhảy qua quân khác. Quân phong cấp bắt được quân cùng loại.
- Vua đi một ô theo 8 hướng, không bắt quân. Bất kỳ quân địch nào bắt được Vua bằng nước bắt hợp lệ, không xét khắc chế.
- Bắt Vua hoặc loại hết quân địch, tính cả Vua, thì thắng. Hết nước hợp lệ thì thua. Hết đồng hồ hoặc bot lỗi cũng thua.
- Lặp trạng thái ba lần hoặc đạt 500 nước đi hợp lệ thì hòa.

Luật đầy đủ, tọa độ và trạng thái lỗi nằm trong [rules](rules.md).

## Thể thức giải

- Vòng tròn hai lượt. Mỗi cặp thí sinh chơi hai ván và đổi màu.
- Trắng đi quân và đặt quân trước. Mỗi người cầm Trắng một ván trong cặp.
- Lịch dùng phương pháp vòng tròn; thứ tự đăng ký làm thứ tự đầu vào. Ván đầu mỗi cặp cho người đăng ký trước cầm Trắng; ván hai đổi màu. Số thí sinh lẻ tạo một bye mỗi vòng mỗi lượt; bye không được điểm.
- Mỗi ván: thắng 3 điểm, hòa 1, thua 0.
- Xếp hạng: tổng điểm; điểm đối đầu trong nhóm hòa; hiệu số số ván thắng-trận thua; nếu vẫn hòa thì đồng hạng.
- Ban tổ chức chọn đúng một mốc đồng hồ cho giải. Mỗi bên dùng đồng hồ tổng, gồm cả giai đoạn đặt quân. Thời gian biên dịch và thời gian server dừng không tính vào đồng hồ.

## Yêu cầu phi chức năng

- Giao diện web dùng HTTPS same-origin gateway `/api/v1`; không kết nối raw TCP từ trình duyệt. CLI TCP/TLS hiện có được ghi nhận là legacy.
- Bot chỉ giao tiếp qua stdin/stdout; không có mạng. Giới hạn JSON line 1 MiB.
- Source bot tối đa 256 KiB UTF-8, theo giới hạn hiện có.
- Kế thừa runner hiện tại: runtime 1 CPU, 256 MiB RAM, 64 PID; compile 1 CPU, 1.024 MiB RAM và tối đa 15 giây.
- Database lưu giải, phiên bản bot, suất dự thi, trận, trạng thái bàn, đồng hồ và log action.
- Clock đang chạy được checkpoint ít nhất mỗi giây; downtime server không tính vào đồng hồ bot.
- Event log giữ dữ liệu xếp quân bí mật ở phía server. Không trả vị trí đối thủ trong phase đặt quân.
- Cookie web có `Secure`, `HttpOnly`, `SameSite`; thao tác thay đổi dữ liệu có CSRF protection.
- Cookie dùng `SameSite=Lax`. Mutation cần CSRF token.
- Xóa source, binary, state chi tiết và event log sau 90 ngày từ khi giải kết thúc; giữ kết quả tổng hợp và bảng điểm.

## Ngoài phạm vi OTT v1

- Game editor hoặc nhiều game cấu hình tùy ý.
- Game khác ngoài OTT v1.
- Playoff sau vòng tròn; đồng điểm cuối cùng giữ đồng hạng.
- Chỉnh sửa bot sau khi giải khóa, đổi bot giữa các ván, đội nhiều thành viên.
- Công khai dịch vụ trên Internet.
- Production web/API và bot runner OTT; mockup không phải giao diện production.
- Xóa hoặc thay thế code judge legacy.

## Tiêu chí đạt cho sản phẩm đích

- Thí sinh đăng ký, gửi bot hợp lệ và xem lịch/kết quả qua web.
- Organizer được cấp quyền có thể tạo giải, chọn thời gian, khóa bot và tạo lịch.
- Hai bot chơi đúng luật OTT qua protocol đã công bố.
- Lịch hai lượt đổi màu; bye, điểm và tie-break cho kết quả nhất quán.
- Timeout, action sai, hòa, phong cấp và bắt Vua cho verdict đúng.
- Ván đang chạy khôi phục được từ log; downtime không làm giảm đồng hồ bot.
- Bố trí ẩn không lộ cho đối thủ trước khi phase chơi bắt đầu.

## Tài liệu kiến trúc và prototype

- [Architecture](architecture.md): thành phần, ranh giới và luồng dữ liệu.
- [Web API](web-api.yaml): OpenAPI thiết kế cho gateway; chưa triển khai.
- [Bot protocol schema](bot-protocol.schema.json): schema observation/action JSON Lines.
- [Match lifecycle](match-lifecycle.md): lịch, trạng thái, clock, event và recovery.
- [UI/UX Draft](UI_UX_Draft.md) và [mockup tĩnh](../mockup/index.html): luồng màn hình và prototype.
- [Database schema](database-schema.md) và [VibeSec](VibeSec.md): dữ liệu, quyền và threat model.
