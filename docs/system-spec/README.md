# Hồ sơ đặc tả hệ thống LapTrinhThiDau

Cập nhật: 27/10/2026

Phạm vi: checkout `origin/main` tại thời điểm lập hồ sơ. Tài liệu mô tả mã nguồn và phân biệt chức năng đã có với phần mới ở mức thiết kế hoặc chưa nghiệm thu.

## Bộ tài liệu

1. [Đặc tả yêu cầu hệ thống](requirements.md) — phạm vi, vai trò, luồng nghiệp vụ, quy tắc OTT và tiêu chí chấp nhận.
2. [Kiến trúc và hợp đồng kỹ thuật](architecture-and-interfaces.md) — module, luồng dữ liệu, API, protocol bot, dữ liệu và ranh giới tích hợp.
3. [Bảo mật, vận hành và nghiệm thu](operations-security-acceptance.md) — cấu hình chạy, sandbox, backup, phục hồi, rủi ro và các cổng release còn mở.

## Phạm vi sản phẩm

Repo chứa ba phần liên quan nhưng chưa hợp nhất thành một runtime:

- OTT Arena: ứng dụng web, HTTP API, engine luật, lịch thi đấu, bot runner và SQLite trong `mockup/` và `ott/`.
- Judge TCP/TLS: hệ thống chấm bài lập trình theo test tĩnh trong `judge/`; đây là dịch vụ legacy độc lập.
- Generic Core v2: mô hình cuộc thi, controller, audit/rejudge và adapter trong `core/`; trạng thái tích hợp còn hạn chế, xem [release acceptance](../release-acceptance.md).

## Cách đọc trạng thái

- **Có trong mã nguồn**: hành vi được triển khai trong checkout được mô tả.
- **Thiết kế/hợp đồng**: tài liệu hoặc schema mô tả mục tiêu; chưa đủ bằng chứng để coi là runtime đã nghiệm thu.
- **Chưa nghiệm thu**: cần triển khai hoặc lưu bằng chứng trên môi trường đích. Unit test hiện có không thay thế staging, load, backup/restore hay kiểm thử Docker trên Linux.

Tài liệu được tổng hợp từ mã nguồn và tài liệu hiện có. Các file `docs/PRD.md`, `docs/architecture.md`, `docs/rules.md`, `docs/database-schema.md` và `docs/VibeSec.md` còn một số mô tả trạng thái cũ. Khi cần biết hành vi hiện tại, đối chiếu `ott/`, `mockup/`, `deploy/README.md` và các kiểm thử tương ứng.

Không chạy test hoặc thay đổi mã nguồn trong lần lập hồ sơ này.
