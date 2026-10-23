# Ngôn ngữ và bảo vệ nhiều lớp

## Phạm vi

Trình soạn thảo OTT, API OTT và CLI judge hỗ trợ `cpp17`, `c17`, `rust2021`, `go`, `java17`.
Compiler tương ứng là `g++`, `gcc`, `rustc`, `go`, `javac`. Rust dùng edition 2021,
chỉ thư viện chuẩn; dịch trực tiếp bằng `rustc`, không tải crate từ mạng.
Go tắt cgo, module, proxy và toolchain tự tải. Java dùng JDK 17, tắt annotation processing,
và đóng gói class vào JAR có entry point cố định `Main`. Chỉ thư viện chuẩn được dùng;
compiler container không có mạng.
Các cờ và tên file nằm trong danh sách cố định tại `core/languages.py`.
Client không được truyền lệnh shell, compiler, đường dẫn hoặc cờ tùy ý.

OTT có mẫu `samples/bot_template.cpp`, `samples/bot_template.c`,
`samples/bot_template.rs`, `samples/bot_template.go`, `samples/bot_template.java`.
Mẫu C, Rust, Go và Java chọn action hợp lệ đầu tiên.
Chọn ngôn ngữ trong trình soạn thảo; đổi ngôn ngữ giữ nguyên mã hiện tại.
Dùng **Khôi phục mã mẫu** để lấy mẫu tương ứng.

CLI judge nhận `--language c17`, `--language rust2021`, `--language go` hoặc `--language java17`.
Mặc định vẫn là `cpp17`.

## Tính nhất quán dữ liệu

- Ngôn ngữ được lưu cùng bản nháp và receipt nộp bài bất biến.
- Tự nộp hết giờ dùng ngôn ngữ của bản nháp trong cùng transaction.
- Cache biên dịch và dấu biên dịch thành công phân biệt ngôn ngữ và SHA-256 nguồn.
- Manifest artifact ghi ngôn ngữ, cờ và môi trường compiler; runner đối chiếu trước khi chạy.
- Schema DB nâng lên phiên bản 4; dữ liệu cũ được bổ sung ngôn ngữ `cpp17` khi mở DB.
  Server cũ không được mở DB đã nâng cấp. Manifest C++ cũ vẫn được nhận nếu các kiểm tra khác hợp lệ.
- Mọi ngôn ngữ dùng chung hạn mức 10 lượt biên dịch mỗi entry. Đổi ngôn ngữ không đặt lại hạn mức.

## Các lớp bảo vệ

1. **Gateway:** origin key, HTTPS, origin công khai, cookie và CSRF giữ cơ chế hiện có.
2. **HTTP:** giới hạn header 32 KiB, body theo policy, từ chối header nhạy cảm trùng,
   framing mơ hồ, Transfer-Encoding, request target sai và body trên GET/HEAD/OPTIONS.
   Từ chối mutation mang `Sec-Fetch-Site: cross-site`.
   JSON được kiểm tra và giải mã một lần trong mỗi request; từ chối key trùng,
   số không hữu hạn kể cả exponent tràn, và cấu trúc lồng quá 64 tầng trước khi giải mã.
   Dấu ngoặc, dấu nháy và escape trong chuỗi mã nguồn không được tính là tầng JSON.
3. **Tài khoản:** giới hạn request theo IP và theo tài khoản; quyền đọc nguồn vẫn chỉ thuộc chủ sở hữu.
   Request, xác thực theo IP, đăng nhập theo username, tài khoản và kiểm tra biên dịch dùng năm bộ đếm độc lập.
   Mỗi bộ giữ tối đa 4.096 danh tính; heap hết hạn giữ tối đa 8.192 bản ghi sau mỗi lần cập nhật.
   Khi đầy, danh tính mới nhận 429 cho tới khi có bucket hồi đầy để thu hồi.
   Không xóa bucket còn hạn chế, nên thay đổi hàng loạt danh tính không đặt lại quota của danh tính đã bị chặn.
   Bộ đếm nằm trong RAM của API và được đặt lại khi tiến trình khởi động lại.
4. **Compiler:** container riêng, không mạng, nguồn chỉ đọc, không secret hay Docker socket,
   user không phải root, giới hạn CPU/RAM/PID/thời gian/log/kích thước artifact.
   Thời gian compile 15 giây; Go được 30 giây và `/tmp` 256 MiB; Java được 45 giây và `/tmp` 512 MiB.
5. **Runtime:** image riêng không chứa compiler, không mạng, filesystem chỉ đọc,
   `/tmp` noexec, bỏ capability, no-new-privileges, giới hạn tài nguyên và tắt core dump.
   Docker giữ seccomp mặc định; không dùng privileged hoặc seccomp unconfined.
6. **Host:** `deploy/firewall.nft` mặc định drop input/forward/output; chặn địa chỉ
   loopback giả mạo từ interface khác; chặn toàn bộ forwarding; chỉ mở egress DNS/NTP/HTTPS/Tunnel
   tới IP trong set riêng được duyệt. Sáu set IPv4/IPv6 rỗng mặc định chặn kết nối outbound mới.
   Mỗi dịch vụ chỉ nhận cổng cần thiết. Packet bị chặn được đếm và log có giới hạn.
7. **Service:** `deploy/contest.service` hạn chế namespace, kernel, clock, realtime;
   chỉ cho API kết nối IP loopback qua systemd. Tunnel phải chạy ở service riêng.

Judge tĩnh chốt image Linux theo ID khi khởi động, không tự pull; kiểm tra output
compiler là file thường, không symlink/hardlink, nằm trong giới hạn và có định dạng đúng:
ELF cho ngôn ngữ native, JAR có `Main-Class: Main` cho Java.
OTT kiểm tra hash artifact, manifest, định dạng và image ID trước khi chạy.

## Chuẩn bị image và nâng cấp

Chạy từ thư mục dự án trên máy Linux triển khai:

```sh
python -m ott build-image --target compiler --image ott-bot-runner
python -m ott build-image --target runtime --image ott-bot-runtime
docker build -f judge/Dockerfile.runner --target compiler -t lan-judge-cpp17 .
docker build -f judge/Dockerfile.runner --target runtime -t lan-judge-runtime .
```

Release nên dùng base image đã duyệt, ghim digest qua `--base-image` của OTT
hoặc `--build-arg BASE_IMAGE=...@sha256:...` của Docker.
Image compiler cũ cần được build lại để có gcc, rustc, Go và JDK 17; image runtime cần JRE 17.
Phiên bản compiler phụ thuộc image đã build; không hứa hỗ trợ mọi tính năng Rust mới.

Profile OTT demo mặc định dùng `ott-bot-runtime`, yêu cầu compiler/runtime có ID khác nhau
và source allowlist không rỗng. API, worker và các lệnh vận hành phải dùng cùng
`--runtime-image`. Judge server mặc định dùng `lan-judge-runtime`;
có thể cấu hình bằng `--docker-runtime-image`.

Nâng cấp trong cửa sổ bảo trì: xử lý receipt đang chờ, sao lưu DB/artifact,
build image, triển khai đồng bộ API và worker. Image ID thay đổi làm artifact
cũ không còn khớp profile; cần quy trình release tạo phiên bản đã duyệt mới.
Không xóa manifest hoặc tắt kiểm tra để tiếp tục trận đang dở.

## Firewall và giới hạn an toàn

File nftables dành cho VM Linux riêng, dùng console quản trị. `deploy/apply-firewall.sh check`
kiểm cấu hình hiện tại mà không đổi rules; `apply --yes` thay riêng bảng `inet contest_demo`
trong một giao dịch nftables. Unit `deploy/contest-firewall.service` có thể bật riêng để
áp chính sách sau reboot; ứng dụng không tự cài hoặc kích hoạt unit.
Trước khi áp dụng, điền IP vào các set `approved_dns_v4/v6`, `approved_ntp_v4/v6`
và `approved_tunnel_v4/v6`; rà quyền quản trị của VM. Set rỗng chặn kết nối outbound mới.
Duy trì danh sách khi endpoint nhà cung cấp thay đổi. Dùng
`sudo sh deploy/apply-firewall.sh check` để kiểm tra cấu hình. Không dùng `flush ruleset`.
Rollback riêng bảng này bằng `sudo nft delete table inet contest_demo`.
Forwarding và Docker published ports bị chặn trên VM này; chuẩn bị image trước khi áp firewall.

Các lớp trên giảm rủi ro, không bảo đảm an toàn tuyệt đối. Container dùng chung kernel;
tài khoản có Docker socket có quyền rất lớn trên host. Giữ VM riêng, cập nhật OS,
Docker và compiler, duy trì source allowlist demo. Local runner chạy mã trực tiếp
trên host, chỉ dành cho development với mã tin cậy.
Giới hạn IP của systemd cần Linux/cgroup-BPF hỗ trợ; phải xác nhận trên máy triển khai.

Tham khảo chính thức: [rustc flags](https://doc.rust-lang.org/rustc/command-line-arguments.html),
[Docker seccomp](https://docs.docker.com/engine/security/seccomp/),
[systemd resource control](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html),
[nftables](https://wiki.nftables.org/wiki-nftables/index.php/Main_Page).
