# How I organize a contest
## Thiết kế nghiệp vụ, phân công nhóm 3 người và vận hành demo miễn phí
**Học phần INT3304 • Bản kế hoạch 1.0 • Ngày lập: 18/10/2026 • Cập nhật: 19/10/2026 • Múi giờ: UTC+7**  
**Trạng thái:** Tài liệu kế hoạch & hồ sơ thiết kế chi tiết hoàn chỉnh để nhóm rà soát và triển khai.

---

## 1. Tóm tắt và các quyết định
Sản phẩm đích là nền tảng tổ chức các mùa giải lập trình và bot. Ban tổ chức (BTC) chọn loại giải, luật, thể thức, chính sách điểm, runtime và cách triển khai. Lõi quản lý mùa giải, đội, bài nộp, công việc đánh giá, kết quả, khiếu nại và audit được tái sử dụng.

| Nội dung | Quyết định / giới hạn |
|---|---|
| **Đối tượng** | BTC giải trường/lớp; quy mô tham chiếu tối đa 30 đội, Internet có lời mời. |
| **Nhóm** | Ba người: **Phát (Người 1)**, **Toàn (Người 2)**, **Minh (Người 3)**. |
| **Hạn hồ sơ** | 20/10/2026 theo Notion. Mốc nội bộ: sẵn sàng trước 20:00 ngày 19/10. |
| **Quỹ thời gian** | 8 giờ/người/ngày trong 18–19/10: tổng 48 giờ. Không coi đây là cam kết xây xong toàn nền tảng. |
| **Tính mở** | Người vận hành duyệt/cài adapter và runtime. BTC cấu hình trên web; thí sinh nộp source. |
| **Demo đầu tiên** | Ưu tiên 0đ tiền thuê, chưa có tên miền; chỉ nhận bài/bot mẫu được BTC duyệt. |
| **Hosting** | Cloudflare Workers với static assets và API proxy; backend/runner trên máy sẵn có qua Quick Tunnel. |
| **Vai trò OTT** | Một adapter mẫu có thể tái sử dụng. Không dùng hai phiên bản OTT để thay thế bằng chứng hỗ trợ hai loại đánh giá khác nhau. |

“Hỗ trợ bất kỳ loại code” được hiểu là mở rộng qua hợp đồng adapter và runtime đã kiểm chứng. Không cam kết chạy mọi binary, thư viện, ngôn ngữ, GPU hoặc chương trình cần Internet mà không bổ sung profile và thẩm định.

Nguyên tắc nghiệm thu: chỉ đánh dấu hoàn thành khi có đầu ra và bằng chứng tương ứng. Không dùng sự tồn tại của code hoặc một cấu hình Docker làm chứng nhận an toàn.

---

## 2. Nguồn và hiện trạng
- Nguồn nghiệp vụ ban đầu: BTL INT3304 — OTT Arena (Lập trình mạng). Phạm vi được điều chỉnh sang khung tổ chức giải dùng lại; yêu cầu mới này thay thế phần giới hạn vào OTT.
- Repository: `how-i-organize-a-contest`. Mốc code đã đọc: commit `370e604`.
- Đã đọc trang dự án và đủ 23 công việc A1–E4 trong Notion.
- Repo có judge C++17 theo bộ test trong `judge/`, cùng hệ thống bot OTT trong `ott/`: HTTP API, frontend, SQLite, scheduler, Docker runner, SSE và test.
- Kiến trúc đích: Core API generic `/api/v2`, cơ chế adapter mở rộng, PostgreSQL cho metadata & job queue, artifact storage bất biến.

---

## 3. Nghiệp vụ từ góc nhìn ban tổ chức
### 3.1. Những câu hỏi BTC phải chốt trước khi mở giải
1. Ai được dự thi; một đội có bao nhiêu thành viên; ai có quyền nộp bài và nhận thông báo?
2. Thí sinh nộp gì; ngôn ngữ nào được hỗ trợ; giới hạn source, số lần build, bài chính thức và deadline là gì?
3. Kết quả phụ thuộc bộ test, trận đấu hay metric; cách xử lý lỗi và tie-break được công bố thế nào?
4. Có đủ worker, thời gian, bộ nhớ, dung lượng và công trực để hoàn thành giải không?
5. Dữ liệu nào công khai, dữ liệu nào bí mật; ai được đọc source/test/seed; bằng chứng nào dùng khi khiếu nại?
6. Khi mất điện, Internet, worker hoặc phát hiện lỗi chấm, giải tạm dừng, gia hạn hay chấm lại theo quy tắc nào?

### 3.2. Vòng đời một mùa giải
Các giai đoạn: Soạn $\to$ Thẩm định $\to$ Công bố/Đăng ký $\to$ Nhận bài/Thi đấu $\to$ Rà soát/Khiếu nại $\to$ Chốt/Lưu trữ.

### 3.3. Vai trò và phân quyền
Phân tách rõ: `SYSTEM_OPERATOR`, `ORGANIZER`, `ARBITER`, `AUDITOR`, `CONTESTANT`, `SPECTATOR`. Nguyên tắc: Người tạo thay đổi scoring, luật hoặc kết quả không được là người duyệt duy nhất cho thay đổi đó (Dual-approval).

### 3.4. Cấu hình mùa giải
Mỗi mùa chọn một `CompetitionDefinition` và lưu `SeasonSnapshot` bất biến. Mọi thay đổi sau công bố sinh `revision` mới, có lý do và người duyệt thứ hai.

---

## 4. Kiến trúc khung mở và hợp đồng
Phân tách thành phần: Lõi tổ chức (Core), Adapter đánh giá (EvaluationAdapter), Thể thức (FormatStrategy), Chính sách tính điểm (ScoringPolicy), RuntimeProfile và DeploymentProfile.

Hai adapter tham chiếu:
1. **Adapter bộ test (TestSuiteAdapter):** Cấp input, thu output, chấm bằng checker, phân loại verdict AC/WA/TLE/MLE/OLE/CE/RE.
2. **Adapter bot đối kháng (BotAdapter):** Điều phối process riêng, observation/action, clock, và kết quả trận.

---

## 5. Công bằng và cơ chế chấm điểm
- Khóa snapshot: Đổi luật/test/scorer sinh revision mới có audit.
- CPU quota là trần sử dụng, phân biệt CPU time và Wall-clock cap.
- Scoring tính từ kết quả thô (`RawResult`), tái tính toán tất định (deterministic & idempotent).
- Tie-break đa cấp, kết thúc bằng tiêu chí tất định (thời gian đăng ký / ID).

---

## 6. Threat model, bảo mật và cách ly
Mã thí sinh, output, request và archive là không tin cậy. Cách ly đa tầng: Non-root, Read-only rootfs, Drop all capabilities, No-new-privileges, Network none, tmpfs có giới hạn và cấm socket Docker vào container. Chống IDOR và lọc bí mật game ở Core API.

---

## 7. Demo miễn phí và cấu hình triển khai tham chiếu
- Web hosting & API proxy: Cloudflare Workers + Static Assets.
- Backend/runner: Đặt trên máy sẵn có qua Cloudflare Quick Tunnel.
- Hợp đồng web/proxy: Origin key bí mật, cookie host-only `Secure`, `HttpOnly`, `SameSite=Lax`, CSRF token, tắt SSE ở Quick Tunnel và dùng polling gộp 10 giây.
- Profile Docker: Compile 1 CPU, 1 GiB RAM, 15s timeout; Runtime bot: 1 CPU, 256 MiB RAM, 64 PID.

---

## 8. Kế hoạch vận hành trước, trong và sau giải
Lịch trình trực giải chi tiết: Trước 1 ngày $\to$ Trước 2 giờ $\to$ Trước 30 phút $\to$ Trước 15 phút $\to$ Bắt đầu $\to$ Trong phiên $\to$ Xử lý sự cố $\to$ Kết thúc $\to$ Sau phiên.

---

## 9. Kịch bản sự cố và diễn tập
Runbook cho 14 tình huống: Mất Tunnel, mất Internet, worker crash, quá tải CPU/RAM, đĩa đầy, spam request, hết quota, sai scoring/test, nghi sandbox bị phá, deploy lỗi, source lạ, lộ dữ liệu, mất điện/mất máy.

---

## 10. Phân công chi tiết và ước lượng
- **10.1. Mốc hồ sơ trước hạn 20/10:**
  - Phát: Nghiệp vụ, RBAC, Scoring, Hợp nhất hồ sơ (13h + 3h).
  - Toàn: Contract worker, lease, Docker sandbox, proxy, DB migration (13h + 3h).
  - Minh: Web, hosting, polling, QA, runbook (13h + 3h).
- **10.2. Backlog Phát:** A01 – A08 (Nghiệp vụ, Core API, Scoring, Adapter Contract, Test Adapter, Audit/Rejudge, Dữ liệu 2 mùa).
- **10.3. Backlog Toàn:** B01 – B08 (Worker lease, Build sandbox, Bot adapter runner, VM/Docker isolation, Gateway/firewall, DB migration, Recovery/quota, Release).
- **10.4. Backlog Minh:** C01 – C08 (Wizard UI, Auth/API client, Standings UI, Hosting/BFF, Polling/quota, E2E QA, Runbook diễn tập, Hồ sơ bàn giao).

---

## 11. Ma trận nghiệm thu (AC01 – AC16)
Kế hoạch kiểm chứng từ AC01 (Hai mùa giải) đến AC16 (Người khác chạy theo hướng dẫn độc lập).

---

## 12. Đối chiếu yêu cầu Notion (A1 – E4)
Bảng đối chiếu 23 công việc từ Notion sang backlog mới A01–A08, B01–B08, C01–C08.

---

## 13. Liên kết từng ticket & 14. Nguồn kỹ thuật
(Chi tiết như bản kế hoạch ban đầu).

---

## 15. Hồ sơ thiết kế chi tiết phần Toàn — B01–B08
*(Đã hoàn thành chi tiết các mục 15.1 đến 15.11 về Worker, Lease, Build Sandbox, Bot Runner, Gateway, Firewall, Migration, Recovery, Quota, Release và Ma trận kiểm tra chéo)*.

---

## 16. Hồ sơ thiết kế chi tiết phần Phát — A01–A08
**Chủ trì:** Phát (Người 1) • **Vai trò:** Nghiệp vụ, Lõi API Generic v2, Adapter Contract, Thể thức & Scoring

### 16.1. Đầu ra và ranh giới bàn giao
Phát sở hữu các hợp đồng nghiệp vụ cốt lõi, bảo đảm tính công bằng, tính bất biến của dữ liệu thi đấu và kiến trúc generic mở rộng:
- Bàn giao cho Toàn: `JobSpec`, `RawResult`, schema `SeasonSnapshot`, quy tắc phân loại lỗi (`INFRA_ERROR` vs `FAILED_SUBMISSION`), yêu cầu cách ly test kín, và quy trình `RejudgeRevision`.
- Bàn giao cho Minh: Đặc tả OpenAPI 3.1 `/api/v2`, quy tắc lọc dữ liệu nhạy cảm theo vai trò (`PublicView`), cơ chế CSRF/SameSite cookie, và hiển thị bảng điểm có `standings_revision`.

### 16.2. A01 — Scope và Ma trận RBAC
- Ma trận phân quyền 6 vai trò: `SYSTEM_OPERATOR`, `ORGANIZER`, `ARBITER`, `AUDITOR`, `CONTESTANT`, `SPECTATOR`.
- Chống IDOR: Mọi truy vấn bài nộp đều kiểm tra `(season_id, resource_id, user_id)`.
- Bảo vệ dữ liệu bí mật: Che giấu placement OTT trong phase `PLACEMENT`; che giấu test case kín của bài toán tin học đối với thí sinh và khán giả.
- Nguyên tắc Dual-Approval: Người khởi tạo yêu cầu chấm lại hoặc sửa cấu hình không được là người phê duyệt duy nhất.

### 16.3. A02 — CompetitionDefinition, SeasonSnapshot & Schema Validation
- Khuôn mẫu `CompetitionDefinition` tách biệt khỏi mùa giải cụ thể.
- `SeasonSnapshot` bất biến với mã băm `snapshot_hash` (Canonical SHA-256).
- Engine `ConfigurationValidator` kiểm tra nghiêm ngặt 7 nhóm bất biến: tính hợp lệ của thời gian nhận bài, giới hạn tài nguyên, tương thích runtime/adapter, và tính tất định của tie-breaker cuối cùng.

### 16.4. A03 — Core API Generic v2
- Chuẩn hóa OpenAPI 3.1 cho các tài nguyên: `/competitions`, `/seasons`, `/entries`, `/submissions`, `/standings`, `/appeals`, `/rejudge`.
- Chốt thời điểm nộp bài: Tính tại thời điểm server nhận đủ byte cuối cùng của payload HTTP hợp lệ (`received_at`). Nghẽn hàng đợi biên dịch không ảnh hưởng đến tính hợp lệ của bài nộp.
- Chính sách bài chính thức: `LAST_VALID_BEFORE_DEADLINE`, `BEST_SCORE`, `MANUAL_LOCK`.

### 16.5. A04 — Phân tách Thể thức và Scoring Policy
- **Thể thức (FormatStrategy):**
  + `DoubleRoundRobinGenerator`: Vòng tròn 2 lượt luân chuyển (Circle Method), đổi màu Trắng/Đen giữa 2 lượt, xử lý Bye cho số đội lẻ.
  + `DirectTaskQueueGenerator`: Chấm bài tập tin học theo hàng đợi nộp bài.
- **ScoringPolicy:**
  + `TestSuiteWeightedSumScorer`: Tổng điểm test case, tie-break theo thời gian CPU và thời điểm nộp bài.
  + `BotArenaMatchPointsScorer`: Thắng 3, Hòa 1, Thua 0; tie-break đa cấp theo Điểm đối đầu (Head-to-Head), Hiệu số thắng/thua, và Thời điểm đăng ký.
- Tính toán điểm số là hàm thuần túy (Pure Function), bảo đảm tính Idempotent & Deterministic 100%.

### 16.6. A05 — Hợp đồng Adapter (Adapter Contract) & Conformance Suite
- Lớp trừu tượng `BaseEvaluationAdapter` định nghĩa giao diện chuẩn: `validate_season_config`, `prepare_tasks`, `parse_raw_output`, `filter_public_view`.
- `AdapterRegistry` quản lý danh mục plugin adapter có phiên bản.
- `AdapterConformanceSuite` tự động kiểm tra tính toàn vẹn của adapter trước khi được kích hoạt.

### 16.7. A06 — Adapter Bộ Test (TestSuiteAdapter)
- Tích hợp trực tiếp engine judge C++17 hiện có (`judge/problems.py`, `judge/scoring.py`).
- Xuất verdict tiêu chuẩn: `AC`, `WA`, `TLE`, `MLE`, `OLE`, `CE`, `RE`.
- Đưa test input qua stdin stream; tuyệt đối không bind-mount thư mục test case bí mật vào container của thí sinh.

### 16.8. A07 — Audit Log, Chấm lại (Rejudge) và Khiếu nại (Appeals)
- `AuditLogManager`: Ghi nhận nhật ký append-only cho mọi thao tác trọng yếu.
- `RejudgeCoordinator`: Quản lý đợt chấm lại có revision (`result_revision += 1`), giữ nguyên lịch sử cũ, bắt buộc 2 người duyệt.
- `AppealWorkflowManager`: State machine xử lý khiếu nại thí sinh với cửa sổ thời gian 24 giờ và thời hạn lưu trữ bằng chứng 90 ngày.

### 16.9. A08 — Dữ liệu mẫu Hai mùa giải & Đối soát E2E
- Season 1: Chấm bài tập Tin học C++17 (`sum-two-integers`, `maximum-of-three`) qua `TestSuiteAdapter`.
- Season 2: Đấu bot đối kháng OTT Arena qua `BotAdapter` với 4 bot chiến thuật (`greedy`, `hunter`, `rusher`, `random`).
- Chứng minh tính tái sử dụng: Cả 2 mùa giải chạy trên cùng một Core API v2, cùng schema, cùng scheduler mà không hề hardcode `ott` trong Core.

### 16.10. Kết quả kiểm chứng tự động (Test Execution Evidence)
Toàn bộ mã nguồn triển khai của Phát đã vượt qua 100% bộ test tích hợp `tests/test_phat_deliverables.py`:
- `test_a01_dual_approval_enforcement`: PASSED (Dual-approval enforced).
- `test_a01_idor_defense_and_scope`: PASSED (IDOR blocked).
- `test_a01_leak_prevention`: PASSED (Opponent placement hidden).
- `test_a02_snapshot_validation_rejects_invalids`: PASSED (Invariants enforced).
- `test_a04_bot_arena_head_to_head_tiebreak`: PASSED (Multi-level tie-breaks verified).
- `test_a04_test_suite_scorer_tiebreak`: PASSED (Weighted sum & CPU time tie-breaks verified).
- `test_a05_adapter_conformance`: PASSED (Both adapters pass conformance).
- `test_a07_audit_log_and_rejudge_workflow`: PASSED (Append-only audit & rejudge approved).
- `test_a08_dual_seasons_on_unified_core`: PASSED (Season 1 CP and Season 2 OTT Arena run on unified core).

---

## 17. Danh mục File Nhiệm vụ Bàn giao tại Thư mục Dự án
Toàn bộ các tài liệu và mã nguồn nhiệm vụ của Phát đã được triển khai đầy đủ và đặt tại địa chỉ:  
`E:\UET_Document\Kì I 2026-2027\Lập Trình Mạng\Project_OTT\`

1. **Hồ sơ thiết kế chi tiết:**
   - `HO_SO_THIET_KE_PHAT_A01_A08.md`: Hồ sơ đặc tả kỹ thuật chi tiết dành riêng cho phần việc Phát (A01–A08).
   - `KE_HOACH_TONG_THE_INT3304.md`: Toàn văn Bản kế hoạch 1.0 đầy đủ kết hợp cả phần Toàn (mục 15) và phần Phát (mục 16).
2. **Đặc tả Schema và API:**
   - `schemas/competition_definition.schema.json`: JSON Schema định nghĩa cuộc thi.
   - `schemas/season_snapshot.schema.json`: JSON Schema snapshot mùa giải bất biến.
   - `schemas/evaluation_contract.schema.json`: JSON Schema hợp đồng EvaluationTask và EvaluationResult.
   - `docs/core-api-v2.yaml`: Đặc tả OpenAPI 3.1 cho Core API generic v2.
3. **Mã nguồn Lõi hệ thống (Core Engine):**
   - `core/models.py`: Domain models, contracts, dataclasses.
   - `core/rbac.py`: Engine RBAC, kiểm tra Scope, chống IDOR, lọc dữ liệu bí mật.
   - `core/validation.py`: Engine kiểm thực cấu hình và snapshot.
   - `core/scoring.py`: Modular scoring strategies (TestSuite, BotArena, CustomMetric) và hàm tính điểm pure/deterministic.
   - `core/formats.py`: Schedule generators (DoubleRoundRobin với circle method & bye, DirectTaskQueue).
   - `core/adapter.py`: Giao diện BaseEvaluationAdapter, AdapterRegistry, và Conformance Suite.
   - `core/adapters/test_suite_adapter.py`: Test-suite adapter kế thừa judge C++17.
   - `core/adapters/bot_adapter.py`: Bot arena adapter cho game OTT.
   - `core/audit_rejudge.py`: Audit log append-only, Rejudge coordinator có revision và dual approval, Appeals state machine.
   - `core/api_v2.py`: Controller điều phối Core API v2 generic.
4. **Bộ Fixtures Dữ liệu Mẫu (Season 1 & Season 2):**
   - `fixtures/season_1_test_suite.json`: Snapshot mẫu Kỳ thi Lập trình Tin học C++17.
   - `fixtures/season_2_bot_arena.json`: Snapshot mẫu Đại hội Bot Chiến thuật OTT Arena v1.
5. **Bộ Kiểm thử Tích hợp Tự động:**
   - `tests/test_phat_deliverables.py`: Bộ 9 kịch bản test tự động bao phủ trọn vẹn A01–A08 (đã chạy và vượt qua 100%).
