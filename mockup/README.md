# OTT Arena web client

Web client giao diện kiểu Lichess (theme Dark), gọi API thật tại `/api/v1`. Không còn dữ liệu mẫu: mọi màn hình đọc từ backend `ott/`. Vanilla JS (ES module), không build step; backend phục vụ thư mục này làm file tĩnh.

Chạy cả backend lẫn web từ thư mục gốc `laptrinhthidau/`:

    python -m ott seed      # tạo 1 organizer + 4 thí sinh; mật khẩu sinh ngẫu nhiên, ghi vào data/seed-accounts.json
    python -m ott serve     # http://127.0.0.1:8080

Route (hash): `#/` màn chính, `#/tournaments`, `#/tournament/:id`, `#/code/:id` (viết bot + đếm ngược), `#/game/:matchId` (ván của tôi hoặc xem ván đội khác), `#/watch` (lưới nhiều ván), `#/bot`, `#/user/:name`, `#/organizer`, `#/organizer/new`, `#/organizer/manage/:id`, `#/rules`, `#/login`, `#/register`.

Phím tắt trang ván: ←/→/↑/↓ duyệt nước, `f` xoay bàn.

Logo: Tabler Icons "chess-knight" (MIT), `assets/knight-tabler-MIT.svg`. Quân Kéo/Búa/Bao/Vua là SVG tự vẽ; không dùng asset Lichess. Ảnh chụp màn hình ở `screenshots/`.
