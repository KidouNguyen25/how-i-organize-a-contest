# UI/UX Draft: Nền tảng thi đấu bot OTT

## Trạng thái và nguyên tắc

Đây là bản nháp cho sản phẩm web tương lai. Repo có [mockup HTML tĩnh](../mockup/index.html) chạy bằng dữ liệu mẫu; mockup không gọi API và không lưu dữ liệu. Code production hiện chỉ có CLI judge theo test tĩnh; chưa có tournament UI hoặc OTT engine.

Giao diện tiếng Việt, ưu tiên desktop và hỗ trợ màn hình nhỏ. Có nhóm thí sinh và organizer; người vận hành cấp quyền organizer ngoài luồng sản phẩm. Chỉ organizer được cấp quyền mới thấy màn quản lý giải. Trình duyệt dùng HTTPS same-origin gateway, cookie `Secure`, `HttpOnly`, `SameSite=Lax` và CSRF token.

## Điều hướng

- **Giải đấu:** danh sách giải, trạng thái đăng ký và luật OTT.
- **Giải của tôi:** suất dự thi, phiên bản bot, lịch và kết quả.
- **Bảng xếp hạng:** điểm và tie-break.
- **Quản lý giải:** chỉ hiện với vai trò organizer.
- **Tài khoản:** đăng nhập, đăng ký và đăng xuất.

## Màn thí sinh

### Danh sách và chi tiết giải

Danh sách hiển thị tên giải, game/version, trạng thái, đồng hồ, số suất và nút xem chi tiết. Trang chi tiết hiển thị luật OTT, giới hạn bot, thời gian đăng ký, lịch và bảng điểm.

```text
┌──────────────────────────────────────────────────────────────┐
│ OTT League 2026     Giải đấu   Giải của tôi   Tài khoản     │
├──────────────────────────────────────────────────────────────┤
│ OTT v1 · Đăng ký mở · 10 phút mỗi bên                       │
│ Vòng tròn hai lượt · C++17                                  │
│ [Xem luật]                                      [Đăng ký]    │
└──────────────────────────────────────────────────────────────┘
```

Một tài khoản chỉ có một suất trong giải. Nút đăng ký yêu cầu đăng nhập. Nếu giải đã khóa, nút đăng ký và thay bot bị tắt.

### Gửi và chốt bot

Trang suất dự thi có vùng tải/chọn source C++17, trạng thái biên dịch và phiên bản đang chọn. Mỗi lần gửi tạo phiên bản bất biến mới. Thí sinh có thể chọn phiên bản khác trước khi giải khóa. Khi khóa, giao diện hiển thị rõ phiên bản đã chốt và không cho thay đổi.

```text
Bot dự thi: OTT v1
Ngôn ngữ: C++17
Phiên bản đang chọn: bot-03 · Biên dịch thành công
[Gửi phiên bản mới]                         [Chọn phiên bản]
```

Bot lỗi biên dịch không thể được chọn để khóa giải. Hiển thị lỗi compiler đã lọc thông tin hệ thống nhạy cảm.

### Lịch và bảng điểm

Lịch hiển thị vòng, lượt, đối thủ, màu, trạng thái và kết quả. Mỗi cặp có hai ván đổi màu. Bye hiển thị thành dòng riêng, không cộng điểm.

Bảng xếp hạng hiển thị thứ hạng, thí sinh, tổng điểm, điểm đối đầu, hiệu số và số ván. Nhấn vào trận để xem kết quả, lý do kết thúc, đồng hồ cuối và trạng thái trận. Không hiển thị log xếp quân bí mật trước khi bố trí được lộ.

### Dashboard và chi tiết trận

Dashboard ưu tiên giải thí sinh đang tham gia, trận tiếp theo, tiến độ, bảng điểm rút gọn và mốc thời gian. Trang chi tiết trận hiển thị kết quả, lý do kết thúc, clock cuối và bàn cờ sau reveal. History placement chỉ xuất hiện sau khi phase chơi bắt đầu.

## Màn organizer

Organizer được người vận hành cấp role. Màn quản lý gồm:

1. Tạo giải OTT v1, nhập tên, chọn 5 phút/10 phút/1 giờ mỗi bên.
2. Mở đăng ký và theo dõi số suất cùng trạng thái compile.
3. Khóa đăng ký; kiểm tra mỗi thí sinh đã chọn bot biên dịch thành công.
4. Sinh lịch hai lượt và bắt đầu hàng đợi trận.
5. Theo dõi tiến độ, lỗi hạ tầng, kết quả và bảng xếp hạng.

Sau khi giải khóa, không sửa luật, đồng hồ, danh sách suất hoặc phiên bản bot. Lỗi hạ tầng tạm dừng trận để khôi phục, không xử thua bot.

Organizer không xem source bot. Trang roster chỉ hiển thị thí sinh, bot version và compile status. Organizer không xem placement bí mật trước reveal.

```text
┌──────────────────────────────────────────────────────────────┐
│ Quản lý giải: OTT League                                    │
├──────────────────────────────────────────────────────────────┤
│ Trạng thái: Đăng ký mở       Đồng hồ: 10 phút mỗi bên       │
│ Suất hợp lệ: 12               Suất chờ sửa bot: 1            │
│ [Khóa đăng ký và tạo lịch]                                  │
├──────────────────────────────────────────────────────────────┤
│ Vòng 1 · 6 trận       Vòng 2 · 6 trận       Trạng thái ...   │
└──────────────────────────────────────────────────────────────┘
```

## Trạng thái và khả năng tiếp cận

- Trạng thái: chưa đăng nhập, đăng ký mở/đóng, source đang tải, compile thành công/lỗi, chờ khóa, đã khóa, trận đang chạy/tạm dừng/hoàn tất.
- Kết quả trận gồm thắng, hòa, thua, timeout, action sai, bot crash, bắt Vua, loại hết quân hoặc lỗi hệ thống.
- Lỗi đăng nhập không tiết lộ username có tồn tại hay không. Session hết hạn chuyển người dùng về đăng nhập và giữ đường dẫn hiện tại.
- Điều hướng bằng bàn phím, focus nhìn thấy được, nhãn input đầy đủ; verdict và màu quân không chỉ phân biệt bằng màu.
- Lưới 9×9 có nhãn tọa độ. Màn nhỏ cho phép cuộn bàn cờ và lịch đấu ngang.
- Mockup có nút mô phỏng đăng ký, chọn bot, khóa roster, tạo lịch và bắt đầu giải. Trạng thái chỉ tồn tại trong phiên trình duyệt.
- Hướng dẫn mở mockup nằm trong [mockup README](../mockup/README.md). Luồng API đích xem tại [OpenAPI](web-api.yaml); contract bot xem tại [JSON Schema](bot-protocol.schema.json).

Luật và bot API xem tại [rules](rules.md). Mục tiêu và vai trò xem tại [PRD](PRD.md). Kiến trúc và vòng đời trận xem tại [architecture](architecture.md) và [match lifecycle](match-lifecycle.md).
