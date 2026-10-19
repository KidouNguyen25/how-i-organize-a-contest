---

## 🚀 Kiến trúc Lõi Nền tảng v2 (Generic Core Platform)

> **Phụ trách thiết kế & phát triển:** Phát (Người 1)  
> **Phạm vi hoàn thành:** Gói nhiệm vụ **A01 – A08** theo Bản kế hoạch 1.0 (INT3304)  
> **Kiểm thử tự động:** 100% Pass (`tests/test_phat_deliverables.py`)

Nền tảng đã được nâng cấp từ một hệ thống gắn cứng với game OTT thành một **khung tổ chức thi đấu mở (Generic Contest Platform)**, cho phép Ban tổ chức (BTC) tổ chức cả các kỳ thi lập trình thuật toán truyền thống (bộ test tĩnh) lẫn các giải đấu bot đối kháng tương tác trên cùng một hệ thống lõi.

---

### 1. Các thành phần chính (Backlog A01 – A08)

| Mã | Hạng mục | Mô tả kỹ thuật |
|---|---|---|
| **A01** | **Scope & RBAC Engine** | Ma trận phân quyền đa vai trò, đa mùa giải (`ORGANIZER`, `ARBITER`, `CONTESTANT`, `SPECTATOR`). Ngăn chặn triệt để lỗ hổng IDOR; che giấu quân cờ bí mật trong phase `PLACEMENT` và bảo vệ bộ test kín; áp dụng cơ chế **Dual-Approval** (hai người duyệt) cho các quyết định trọng yếu. |
| **A02** | **Snapshot & Validation** | Quản lý khuôn mẫu `CompetitionDefinition` và ảnh chụp cấu hình mùa giải `SeasonSnapshot` bất biến với mã băm Canonical SHA-256. Bộ kiểm thực tự động chặn các cấu hình sai lệch (thời gian mâu thuẫn, quota không hợp lệ, tie-breaker không xác định). |
| **A03** | **Core API v2 Generic** | Chuẩn hóa toàn bộ API `/api/v2` theo chuẩn **OpenAPI 3.1**. Chốt thời điểm nộp bài căn cứ vào **Server Receipt Timestamp** (hàng đợi worker bị nghẽn không làm trễ hạn nộp bài); hỗ trợ các chính sách chọn bài chính thức (`LAST_VALID_BEFORE_DEADLINE`, `BEST_SCORE`). |
| **A04** | **Thể thức & Scoring Policy** | Tách biệt triệt để giữa **Format Strategy** (Vòng tròn 2 lượt luân chuyển - Double Round-Robin, Hàng đợi chấm trực tiếp) và **Scoring Policy** (Tổng điểm trọng số bộ test, Điểm trận đấu bot 3-1-0). Thuật toán tính điểm tất định (Deterministic & Idempotent), phân hạng tie-break đa cấp rõ ràng. |
| **A05** | **Hợp đồng Adapter (Contract)** | Định nghĩa giao diện trừu tượng `BaseEvaluationAdapter`, trung tâm quản lý plugin `AdapterRegistry` và bộ kiểm thử tự động `AdapterConformanceSuite` nhằm ngăn chặn các adapter rò rỉ dữ liệu nhạy cảm. |
| **A06** | **Adapter Bộ Test (C++17)** | Tích hợp toàn diện engine chấm bài tập thuật toán sẵn có vào khung Adapter. Chuẩn hóa phân loại verdict (`AC`, `WA`, `TLE`, `MLE`, `OLE`, `CE`, `RE`), cấp test case qua stdin stream cách ly hoàn toàn khỏi container thí sinh. |
| **A07** | **Audit, Chấm lại & Khiếu nại** | Hệ thống ghi nhật ký Audit Log dạng append-only; quy trình kích hoạt đợt chấm lại có revision (`result_revision += 1`) yêu cầu 2 người duyệt; State Machine xử lý khiếu nại (Appeals) với cửa sổ tiếp nhận 24 giờ và lưu trữ bằng chứng 90 ngày. |
| **A08** | **Chứng minh Hai mùa giải mẫu** | Vận hành song song thành công **Season 1 (Kỳ thi Tin học CP)** và **Season 2 (Đại hội Bot OTT Arena)** trên cùng một hệ cơ sở dữ liệu và Core API controller mà không cần sửa đổi mã nguồn lõi. |

---

### 2. Cấu trúc thư mục mã nguồn bàn giao

```text
├── core/                               # Lõi nền tảng thi đấu generic
│   ├── models.py                       # Domain models, dataclasses, snapshot băm SHA-256
│   ├── rbac.py                         # [A01] Engine phân quyền Scope, chống IDOR, lọc bí mật
│   ├── validation.py                   # [A02] Engine kiểm thực cấu hình & Snapshot invariants
│   ├── api_v2.py                       # [A03] Controller điều phối Core API v2
│   ├── scoring.py                      # [A04] Modular Scoring Strategies & Tie-break
│   ├── formats.py                      # [A04] Tournament Format Generators (Round-robin, Queue)
│   ├── adapter.py                      # [A05] BaseEvaluationAdapter, Registry & Conformance
│   ├── audit_rejudge.py                # [A07] Append-only Audit, Dual-approval Rejudge & Appeals
│   └── adapters/
│       ├── test_suite_adapter.py       # [A06] Adapter chấm bộ test C++17
│       └── bot_adapter.py              # [A05/A08] Adapter đấu bot OTT Arena
├── schemas/                            # JSON Schemas chuẩn kiểm thực
│   ├── competition_definition.schema.json
│   ├── season_snapshot.schema.json
│   └── evaluation_contract.schema.json
├── docs/
│   ├── core-api-v2.yaml                # Đặc tả OpenAPI 3.1 cho Core API v2
│   ├── HO_SO_THIET_KE_PHAT_A01_A08.md  # Hồ sơ đặc tả kỹ thuật chi tiết của Phát
│   └── KE_HOACH_TONG_THE_INT3304.md    # Bản kế hoạch tổng thể INT3304 hợp nhất
├── fixtures/                           # Dữ liệu cấu hình mẫu hai mùa giải
│   ├── season_1_test_suite.json        # Mùa 1: Kỳ thi Tin học C++17
│   └── season_2_bot_arena.json         # Mùa 2: Đại hội Bot OTT v1
└── tests/
    └── test_phat_deliverables.py       # Bộ kiểm thử tích hợp tự động cho A01-A08
