# Vòng đời giải và trận OTT

## Trạng thái triển khai

Đây là thiết kế cho OTT v1. Code hiện tại chưa có tournament scheduler, OTT engine hoặc bot runner tương tác. SQLite legacy chỉ phục hồi submission chấm test tĩnh.

## Trạng thái giải

```text
DRAFT → REGISTRATION_OPEN → LOCKED → RUNNING → FINISHED
   └───────────────┴──────────┴──────────────→ CANCELLED
```

- `DRAFT`: organizer cấu hình tên và clock 300, 600 hoặc 3.600 giây mỗi bên.
- `REGISTRATION_OPEN`: thí sinh đăng ký, gửi phiên bản bot và chọn bot compile thành công.
- `LOCKED`: roster và bot versions bất biến; organizer tạo lịch.
- `RUNNING`: scheduler phát các trận đã sinh.
- `FINISHED` và `CANCELLED`: trạng thái kết thúc, không chuyển tiếp.
- Hủy giải chỉ hợp lệ trước `RUNNING`. Sau khi chạy, organizer dừng việc phát trận; trận đang chạy hoàn tất hoặc được operator xử lý sự cố.

## Trạng thái bot và suất

- Bot: `QUEUED → READY` hoặc `QUEUED → CE`. Mỗi upload tạo phiên bản mới.
- Suất: `REGISTERED → READY` khi chọn bot `READY`; có thể `REMOVED` trước `LOCKED`.
- Khi khóa giải, mọi suất phải ở `READY`; user, tournament và bot version phải khớp.
- Bot version không đổi trong giải. Organizer không đọc source; API trả compile status và thông báo compiler đã lọc.

## Trạng thái trận

```text
SCHEDULED → RUNNING ↔ PAUSED → FINISHED
```

- `SCHEDULED`: trận được sinh bởi lịch nhưng chưa có runner.
- `RUNNING`: một runner giữ lease có fencing epoch. Chỉ lease mới nhất được ghi event.
- `PAUSED`: lỗi host, Docker hoặc server. Không xử thua bot và không trừ clock khi hệ thống offline.
- `FINISHED`: result và reason bất biến; standings tính từ các trận hoàn tất.
- Action bot sai, JSON sai, process crash hoặc hết clock kết thúc trận với bot đó thua.

## Lịch deterministic

1. Sắp xếp roster theo `registered_at`, rồi `entry_id` để phá hòa.
2. Dùng circle method tạo một lượt vòng tròn. Với số thí sinh lẻ, thêm slot `BYE`; bye không phải trận và không nhận điểm.
3. Leg 1 dùng roster order để xác định Trắng/Đen. Leg 2 lặp cùng pairing, đảo màu.
4. Lưu `round_no`, `leg_no`, `pairing_no` và hai entry trước khi chuyển giải sang `RUNNING`.

Với `n` thí sinh chẵn, mỗi leg có `n - 1` vòng, mỗi vòng `n / 2` trận. Với `n` lẻ, mỗi leg có `n` vòng, mỗi vòng `(n - 1) / 2` trận và một bye. Mỗi cặp gặp nhau đúng hai lần, một lần mỗi màu.

## Ghi event và snapshot

- `match_events` là log append-only trong thời hạn lưu. Khóa `(match_id, sequence_no)` không cho ghi trùng sequence.
- Sau action hợp lệ, một transaction ghi event, `state_json`, clocks, phase, `side_to_act`, `move_count` và `updated_at`.
- Mỗi runner phải claim lease có epoch tăng đơn điệu. Mọi lần ghi kèm epoch; DB từ chối ghi từ runner cũ.
- Snapshot mang `engine_version` và `state_version`. Recovery dùng snapshot mới nhất đã commit, rồi replay event có sequence lớn hơn snapshot.
- Bot process restart từ đầu. Runner dựng observation từ snapshot và history bot được phép thấy. File ghi được trong container là tạm; bot không được dựa vào file đó để khôi phục.
- Chỉ một action được commit cho mỗi `(match_id, sequence_no)`. Retry sau mất phản hồi đọc lại event đã commit, không áp dụng action lần hai.

## Bí mật placement

- Trong phase `PLACEMENT`, engine lưu full state nội bộ. Observation chỉ gồm quân, số lượng còn lại và history của bot đang tới lượt.
- Không trả raw `match_events`, `state_json` hoặc placement đối thủ qua API organizer hay API người xem trước reveal.
- Khi placement hoàn tất và phase chuyển sang `PLAY`, engine công khai toàn bàn cho hai bot và các màn match được phép xem.
- Sau 90 ngày kể từ khi giải kết thúc, xóa history/state chi tiết. Giữ kết quả cuối, reason, điểm và standings.

## Clock và khôi phục

- Clock tổng 300, 600 hoặc 3.600 giây cho mỗi bên; placement cũng tiêu thời gian.
- Bắt đầu trừ khi engine gửi observation; dừng khi nhận một dòng action hoặc khi runner gặp lỗi hạ tầng.
- Đo thời lượng bằng monotonic clock trong process. Ghi `remaining_ms` và `clock_checkpoint_at` ít nhất mỗi giây.
- Sau restart, dùng clock checkpoint cuối đã commit; không tính thời gian server offline. Sai số mất thời gian tối đa một giây do chu kỳ checkpoint.
- Khi action commit, lưu cả clocks hai bên trong cùng transaction với event và snapshot.
- Nếu clock bằng 0 trước khi action đến, bot đang đi thua do `TIMEOUT`.

## Kết thúc và điểm

- Bắt Vua hoặc loại toàn bộ quân địch, gồm Vua: thắng.
- Không còn nước hợp lệ, timeout, protocol sai, action sai hoặc bot crash: bên đang đi thua.
- Lặp trạng thái gồm bàn cờ, loại/phong cấp quân và bên đi ba lần: hòa. Đạt 500 action đi quân hợp lệ: hòa; placement không tính.
- Thắng 3 điểm, hòa 1, thua 0. Tie-break: tổng điểm, điểm đối đầu trong nhóm hòa, hiệu số thắng-thua toàn giải; nếu còn hòa thì đồng hạng.

## Liên kết

[Rules](rules.md) · [Database schema](database-schema.md) · [Bot protocol](bot-protocol.schema.json) · [Architecture](architecture.md)

