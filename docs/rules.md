# Rules: OTT v1 và giải đấu bot

Tài liệu này là luật đích cho tournament OTT. Code hiện tại chưa có game engine hoặc bot protocol; judge test tĩnh là legacy.

## Tài khoản và giải

- Một tài khoản có một suất trong mỗi giải.
- `CONTESTANT` có thể tham gia và gửi bot. `ORGANIZER` có thể tạo/quản lý giải. Người vận hành cấp role organizer; người dùng không tự nâng quyền.
- Trạng thái giải: `DRAFT`, `REGISTRATION_OPEN`, `LOCKED`, `RUNNING`, `FINISHED`, `CANCELLED`.
- Giải OTT v1 chỉ hỗ trợ bot C++17 và một trong ba đồng hồ: 300, 600 hoặc 3.600 giây mỗi bên.
- Source bot UTF-8 tối đa 256 KiB, cùng giới hạn source của judge hiện có.
- Một suất chọn một phiên bản bot đã compile thành công. Có thể thay lựa chọn trước `LOCKED`; sau đó phiên bản bất biến.
- Cần ít nhất hai suất hợp lệ để bắt đầu giải. Bot lỗi compile chưa đủ điều kiện; thí sinh có thể nộp bản sửa trước khi khóa.

## Bàn cờ và hướng

- Bàn 9×9. Cột `a`–`i`, hàng `1`–`9`. Observation dùng object `board` với key là tọa độ ô, ví dụ `a1`.
- Trắng ở hàng nhà 1–2, tiến về phía đối thủ và hàng 9. Đen ở hàng nhà 8–9, tiến về phía đối thủ và hàng 1.
- Mỗi bên có chính xác 10 quân: 3 Kéo, 3 Búa, 3 Bao, 1 Vua.
- Vị trí có quân đồng minh không phải nước đi hợp lệ. Quân trượt không đi qua ô đang có quân.

## Giai đoạn xếp quân

- Trắng đặt trước. Sau đó hai bên luân phiên, mỗi lượt đặt đúng một quân vào ô trống trong hai hàng nhà của bên mình.
- Mỗi bên đặt đủ 10 quân. Khi xong, engine lật toàn bộ bố trí và bắt đầu phase đi quân.
- Trong phase đặt, mỗi bot chỉ thấy quân mình đã đặt, số quân mình còn lại và ô hợp lệ. Bot không thấy vị trí hoặc loại quân của đối thủ.
- Trắng cũng đi nước đầu tiên. Ván lượt hai trong mỗi cặp đảo màu, nên mỗi thí sinh được đặt trước và đi trước một lần.

## Nước đi và bắt quân

### Kéo, Búa, Bao chưa phong cấp

- Đi đúng một ô theo một trong các hướng: tiến thẳng, chéo tiến trái/phải, chéo lùi trái/phải.
- Không đi ngang hoặc lùi thẳng.
- Đi vào ô trống theo hướng hợp lệ là di chuyển thường.
- Chỉ bắt quân ở ô tiến thẳng hoặc chéo tiến. Không bắt theo hướng chéo lùi.
- Quan hệ bắt: Kéo bắt Bao; Bao bắt Búa; Búa bắt Kéo. Quân thường cùng loại không bắt nhau.

### Phong cấp

- Kéo, Búa hoặc Bao tới hàng cuối của đối thủ phải chọn phong cấp Xe hoặc Tượng trong action đó. Vua không phong cấp.
- Quân phong cấp giữ loại Kéo/Búa/Bao.
- Phong cấp Xe đi mọi ô trống trên hàng/cột không bị chặn; phong cấp Tượng đi mọi ô trống trên đường chéo không bị chặn. Cả hai còn có thể đi đúng một ô liền kề theo bất kỳ 8 hướng.
- Tập đích hợp lệ là hợp của nước trượt và nước một ô; ô trùng chỉ xuất hiện một lần. Quân không nhảy qua quân khác.
- Bắt quân tại một đích hợp lệ theo cùng tập hướng di chuyển. Quan hệ Kéo/Búa/Bao vẫn áp dụng; quân phong cấp còn bắt được quân cùng loại.

### Vua

- Vua đi tối đa một ô theo 8 hướng vào ô trống. Vua không bắt quân.
- Quân địch có thể bắt Vua khi ô Vua nằm trong hướng bắt hợp lệ của quân địch. Bắt Vua không cần xét khắc chế loại.
- Không có luật chiếu hoặc chiếu hết. Engine chỉ áp dụng luật nước đi và bắt được mô tả ở đây.

## Kết thúc ván và lỗi bot

- Bắt Vua hoặc loại hết quân đối phương, tính cả Vua: thắng ngay. Điều kiện loại hết quân bao quát trường hợp không còn quân nào trên bàn.
- Không còn nước hợp lệ khi tới lượt: bên tới lượt thua.
- Mỗi bên có đồng hồ tổng được chọn cho giải; chỉ trừ giờ từ lúc engine gửi observation tới lúc nhận action. Đồng hồ chạy trong lượt đặt và lượt đi của bên đang hành động. Bot process được khởi động trước giờ đầu tiên; biên dịch và thời gian server dừng không tính giờ.
- Khi bot đang nghĩ, server lưu clock checkpoint ít nhất mỗi giây. Khi restart, process mới dựng state từ snapshot và history đã thấy; file ghi trong container là tạm. Thời gian server offline không bị trừ. Sai số tối đa do checkpoint là một giây.
- Chạm 0 ms: thua. Server dừng hoặc khôi phục trận không trừ giờ.
- Mỗi observation cần đúng một dòng action UTF-8 hợp lệ. Dòng quá 1 MiB, JSON lỗi, JSON có key trùng, action thừa, không trả action, action sai schema, action không hợp lệ hoặc process crash: bot đó thua.
- `stdout` chỉ dành cho action. Output chẩn đoán vào `stderr`; output vượt giới hạn runner bị xem là lỗi bot. Engine tự tính và xác thực nước hợp lệ.
- Lặp cùng trạng thái bàn, loại/promote của quân và bên tới lượt ba lần: hòa.
- Sau 500 action đi quân hợp lệ (không tính lượt đặt): hòa.
- Lỗi hạ tầng runner/server làm trận tạm dừng và khôi phục từ log; không tính là lỗi bot.

## Bot protocol v1

Mỗi bot là process C++17 sống trong suốt một ván. Engine chỉ gửi observation khi bot tới lượt. Mỗi observation là một dòng UTF-8 JSON kết thúc bằng LF; bot trả đúng một dòng JSON action. Mỗi dòng tối đa 1 MiB. `stdout` chỉ dành cho protocol; bot ghi chẩn đoán vào `stderr`.

Schema chuẩn cho observation/action là [bot-protocol.schema.json](bot-protocol.schema.json). Parser từ chối key JSON trùng và object có property không được định nghĩa. Action phải khớp đúng một schema `place` hoặc `move`; engine vẫn kiểm tra phase, lượt, tọa độ và luật bàn cờ. Không chấp nhận dòng output thứ hai cho cùng observation.

Observation mẫu:

```json
{
  "protocol_version": 1,
  "match_id": "match-uuid",
  "phase": "placement",
  "side": "white",
  "board": {"a1": {"side": "white", "piece": "scissors", "promotion": null}},
  "remaining_pieces": {"scissors": 2, "hammer": 3, "paper": 3, "king": 1},
  "clocks_ms": {"white": 300000, "black": 300000},
  "legal_actions": [{"type": "place", "piece": "scissors", "to": "b1"}],
  "history": []
}
```

`board` là object theo tọa độ ô có quân; ô trống không có key. Piece ID: `scissors`, `hammer`, `paper`, `king`. `promotion` nhận `rook`, `bishop` hoặc `null`. `history` là mảng object `{ "phase": "placement|play", "side": "white|black", "action": <action> }`. Trong phase đặt, bot chỉ thấy quân đã đặt của mình, quân còn lại của mình và history placement của mình. Organizer và người xem không thấy placement. Engine giữ full state nội bộ. Khi phase chuyển sang `play`, hai bot nhận toàn bộ bàn và history. Khi khôi phục, engine khởi động process mới và gửi lại state cùng history bot được phép thấy. File ghi trong container không phải trạng thái bền vững.

Action hợp lệ có đúng một trong hai dạng:

```json
{"type": "place", "piece": "scissors", "to": "b1"}
```

```json
{"type": "move", "from": "a2", "to": "b3", "promotion": "rook"}
```

`promotion` chỉ có khi quân vừa tới hàng cuối; action không phong cấp bỏ trường này. Engine gửi `legal_actions` để bot chọn; mọi action trả về vẫn được xác thực. Không hỗ trợ pass.

## Lịch vòng tròn và xếp hạng

- Vòng tròn hai lượt: mỗi cặp gặp nhau hai ván; ván hai đổi Trắng/Đen. Trắng luôn đặt trước và đi trước.
- Dùng phương pháp vòng tròn, theo thứ tự đăng ký. Mỗi cặp gặp nhau ở cả hai lượt; ván đầu người đăng ký trước cầm Trắng, ván hai đổi màu. Với số thí sinh lẻ, mỗi vòng mỗi lượt có một bye; bye không nhận điểm.
- Mỗi ván: thắng 3, hòa 1, thua 0.
- Xếp hạng theo thứ tự: tổng điểm; điểm đối đầu của các thí sinh đang bằng điểm; hiệu số số ván thắng trừ số ván thua. Nếu vẫn bằng, cùng hạng.
- Điểm đối đầu là tổng điểm 3/1/0 trong các ván giữa nhóm đang hòa. Hiệu số tính trên toàn giải.

Lịch dùng thứ tự đăng ký ổn định, circle method và slot bye khi số thí sinh lẻ. Leg 2 lặp pairing leg 1 và đảo màu. Bye không phải trận, không có điểm. Định nghĩa đầy đủ và recovery xem tại [match lifecycle](match-lifecycle.md).

## API web và lưu trữ

Web contract đích nằm trong [OpenAPI](web-api.yaml), base path `/api/v1`, HTTPS same-origin. Browser dùng cookie phiên `Secure`, `HttpOnly`, `SameSite=Lax`; mutation cần CSRF token. Chỉ chủ tài khoản xem source bot. Source, binary, state chi tiết và event log được xóa sau 90 ngày từ khi giải kết thúc; kết quả tổng hợp được giữ.

Database đích xem tại [database schema](database-schema.md). Kiến trúc xem tại [architecture](architecture.md). Rủi ro và kiểm soát xem tại [VibeSec](VibeSec.md).
