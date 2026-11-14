# Phase 1 — Authentication Smoke Test Report

**Kết luận: PASS – READY FOR PHASE 1 SIGN-OFF**

57/57 ca smoke test chạy trên server thật đều PASS. 361/361 test tự động pass. Các công cụ chất lượng và bảo mật đều đạt. Đây là **smoke test chức năng**, không thay thế security audit độc lập.

## 1. Thời gian kiểm thử

| Hạng mục | Thời điểm (giờ máy, +07:00) |
|---|---|
| Xác minh môi trường | 2026-11-14, trước 18:23 |
| Smoke test API | 2026-11-14 18:23:56 → 18:24:02 |
| Công cụ chất lượng | 2026-11-14, hoàn tất 18:25:30 |

## 2. Database sử dụng và cách xác minh

- **Database kiểm thử:** `smoke_test.db` (`sqlite+aiosqlite:///./smoke_test.db`).
- **Xác minh server dùng đúng DB** (thực hiện trước mọi thao tác ghi, chỉ đọc):
  1. `GET /health` → `200` (`SELECT 1`), để server mở kết nối DB trong pool.
  2. Windows Restart Manager báo `smoke_test.db` đang bị giữ bởi **PID 8564**, chính là tiến trình lắng nghe `127.0.0.1:8765`.
  3. `ai_video_hub.db` **không bị tiến trình nào mở**.
  4. Script smoke test lặp lại bước 1–3 ngay trước khi chạy và tự dừng nếu không khớp. Lần chạy đầu đã dừng đúng như vậy vì lỗi giải mã output; lần đó không có thao tác ghi nào.
- **Database chính `ai_video_hub.db`:** không bị đọc ghi bởi test. SHA-256 (16 ký tự đầu) `30e019b89985ed45` giống hệt trước và sau smoke test, và sau khi chạy công cụ.
- **Lệnh CLI** (`promote-admin`) chạy với `DATABASE_URL` trỏ tới `smoke_test.db`. Đã kiểm tra cấu hình resolve ra đúng URL trước khi chạy, và output CLI in đúng DB đích.

## 3. Phiên bản migration

- `smoke_test.db`: `0003 (head)` trước và sau test. `alembic check`: "No new upgrade operations detected."
- Head của repo: `0003`.

## 4. Server và môi trường

| Hạng mục | Giá trị |
|---|---|
| Server | `.venv\Scripts\python.exe -m app.server --port 8765` (PID 8564, khởi động 18:15:44), có sẵn, không khởi động thêm server |
| Môi trường | `APP_ENV=development` (theo `/health`), `COOKIE_SECURE=false` nên cookie không có cờ Secure, đúng với môi trường dev |
| Python | 3.12.10 (`.venv`) |
| Branch | `phase-1/authentication-user` |
| Client | `httpx` trong `.venv`, cookie chỉ nằm trong bộ nhớ |
| Bí mật | Mật khẩu sinh ngẫu nhiên trong bộ nhớ (`secrets`), không in, không ghi file; reset token đọc từ outbox vào bộ nhớ, không in |

Tài khoản test (đều `@example.com`): `smoke.user@`, `ratelimit.user@`, `smoke.admin@`; `nobody.smoke@` là email không tồn tại.

## 5–7. Danh sách ca kiểm thử và kết quả

### A. Registration

| # | Ca | HTTP | Kết quả |
|---|---|---|---|
| A1 | Đăng ký hợp lệ | 201 | PASS |
| A2 | Email `Smoke.User@Example.COM` được chuẩn hóa thành chữ thường | 201 | PASS |
| A3 | Response chỉ có field công khai, `role=user`, không có hash | 201 | PASS |
| A4 | Email trùng (khác hoa/thường) | 409 `email_already_registered` | PASS |
| A5 | Mật khẩu < 10 ký tự | 422 | PASS |
| A6 | Mật khẩu chỉ gồm khoảng trắng | 422 | PASS |
| A7 | Email sai định dạng | 422 | PASS |
| A8 | Thiếu `full_name` | 422 | PASS |
| A9 | Tự gửi `role=admin` | 422 | PASS |
| A10 | Lỗi theo định dạng `{"error": {"code", "message"}}` | 422 | PASS |

### B. Login

| # | Ca | HTTP | Kết quả |
|---|---|---|---|
| B1 | Đăng nhập đúng | 200 | PASS |
| B2 | Cookie: `avh_session` (HttpOnly, SameSite=Lax), `avh_csrf` (đọc được từ JS), `avh_device` (HttpOnly) | 200 | PASS |
| B3 | Response không chứa hash hay token | 200 | PASS |
| B4 | Sai mật khẩu | 401 `invalid_credentials` | PASS |
| B5 | Email không tồn tại | 401 `invalid_credentials` | PASS |
| B6 | Body của B4 và B5 giống hệt nhau | — | PASS |
| B7 | Đăng nhập bằng email viết hoa | 200 | PASS |
| B8 | Tài khoản inactive | — | **NOT TESTED** (live): không có API/CLI để khóa tài khoản, và không sửa DB trực tiếp. Đã có test tự động `test_login_rejected_for_inactive_user`, `test_session_of_deactivated_user_is_rejected` (pass) |

### C. Session

| # | Ca | HTTP | Kết quả |
|---|---|---|---|
| C1 | `GET /auth/me` khi đã đăng nhập, đúng email | 200 | PASS |
| C2 | Session được duy trì (gọi lần 2) | 200 | PASS |
| C3 | Không có cookie | 401 | PASS |
| C4 | Dùng lại session đã logout | 401 | PASS |
| C5 | Session giả | 401 | PASS |

### D. CSRF

| # | Ca | HTTP | Kết quả |
|---|---|---|---|
| D1 | `PATCH /users/me` có CSRF hợp lệ | 200 | PASS |
| D2 | Thiếu CSRF | 403 `csrf_failed` | PASS |
| D3 | CSRF sai | 403 `csrf_failed` | PASS |
| D4 | Logout thiếu CSRF | 403 `csrf_failed` | PASS |
| D5 | Session vẫn còn sau D4 | 200 | PASS |

### E. Logout

| # | Ca | HTTP | Kết quả |
|---|---|---|---|
| E1 | Logout có CSRF | 204 | PASS |
| E2 | `avh_session`, `avh_csrf` bị xóa (`Max-Age=0`); `avh_device` giữ lại theo thiết kế | 204 | PASS |
| E3 | `GET /auth/me` sau logout | 401 | PASS |

### F. Rate limiting (theo tài khoản, lưu trong DB)

| # | Ca | HTTP | Kết quả |
|---|---|---|---|
| F1 | 10 lần sai mật khẩu đầu tiên | 401 × 10 | PASS |
| F2 | Lần thứ 11 | 429 `rate_limit_exceeded` | PASS |
| F3 | Có `Retry-After` | 90 giây | PASS |
| F4 | Đúng mật khẩu khi đang bị khóa vẫn bị chặn | 429 | PASS |

Tổng cộng khoảng 20 request login trong cả đợt, dưới ngưỡng 30 lượt mỗi 5 phút theo IP.

### G. Password reset

| # | Ca | HTTP | Kết quả |
|---|---|---|---|
| G1 | Forgot-password cho email tồn tại | 202 | PASS |
| G2 | Status và body giống hệt email không tồn tại | 202 / 202 | PASS |
| G3 | Email reset được ghi vào outbox, có token 43 ký tự (không in) | — | PASS |
| G4 | Reset với token hợp lệ | 200 | PASS |
| G5 | Dùng lại token đã dùng | 400 `invalid_reset_token` | PASS |
| G6 | Token không tồn tại | 400 `invalid_reset_token` | PASS |
| G7 | Hash mới là Argon2id và khác hash cũ (so sánh trong bộ nhớ) | — | PASS |
| G8 | Session cũ bị thu hồi sau reset | 401 | PASS |
| G9 | DB: không còn session hiệu lực của tài khoản | 0 | PASS |
| G10 | Login bằng mật khẩu cũ | 401 | PASS |
| G11 | Login bằng mật khẩu mới | 200 | PASS |
| G12 | Token hết hạn | — | **NOT TESTED** (live): TTL 30 phút, không thể kiểm thử an toàn trong đợt này. Đã có test tự động `test_expired_reset_token_is_rejected` (pass) |

### H. Authorization

| # | Ca | HTTP | Kết quả |
|---|---|---|---|
| H1 | USER gọi `GET /users` | 403 | PASS |
| H2 | USER tự nâng role qua `PATCH /users/me` | 422 | PASS |
| H3 | Chưa đăng nhập gọi `GET /users` | 401 | PASS |
| H4 | `python -m app.cli promote-admin` (cơ chế chính thức) trên `smoke_test.db` | exit 0 | PASS |
| H5 | ADMIN gọi `GET /users` | 200 (total=3) | PASS |
| H6 | Danh sách user không chứa hash | 200 | PASS |

### I. Trusted devices và audit

| # | Ca | HTTP | Kết quả |
|---|---|---|---|
| I1 | Audit `user.role.admin_granted` được ghi đúng user | — | PASS |
| I2 | Audit không chứa email | — | PASS |
| I3 | Đăng nhập lại từ trình duyệt quen | 200 | PASS |
| I4 | Trusted device được tạo | 2 thiết bị | PASS |
| I5 | Trusted device bị thu hồi sau reset | 0 | PASS |
| I6 | Người dùng tự xem hoặc thu hồi trusted device | — | **NOT TESTED**: Phase 1 không có chức năng này; thiết bị chỉ bị thu hồi khi đặt lại mật khẩu (đã kiểm ở I5) |
| I7 | Cookie `Secure` / tiền tố `__Host-` | — | **NOT TESTED** (live): server dev chạy http. Đã có test tự động `test_device_cookie_uses_host_prefix_in_production` (pass) |

| Tổng | PASS | FAIL | NOT TESTED | BLOCKED |
|---|---|---|---|---|
| Ca chạy live | 57 | 0 | — | 0 |
| Ca không chạy live (có test tự động bao phủ hoặc chức năng không tồn tại) | — | — | 4 | — |

## 8. Pytest và coverage

- `pytest`: **361 passed, 0 failed** (61,6 giây).
- Coverage: **95,05%** (ngưỡng 80%). Smart App Control khiến coverage chỉ dùng được tracer Python, nên con số này thấp hơn thực tế (xem CLAUDE.md mục 10).

## 9. Ruff, mypy, Bandit, pip-audit

| Công cụ | Kết quả |
|---|---|
| `ruff check .` | All checks passed |
| `ruff format --check .` | 83 files already formatted |
| `mypy` (strict) | No issues found in 80 source files |
| `bandit -c pyproject.toml -r app` | No issues identified (High 0, Medium 0, Low 0) |
| `pip_audit --skip-editable` | No known vulnerabilities found |

## 10. Alembic

- `alembic current` trên `smoke_test.db`: `0003 (head)`; `alembic check`: không có thay đổi.
- Không chạy migration nào trong đợt này, và không chạy Alembic trên `ai_video_hub.db`.

## 11. Lỗi bảo mật còn tồn tại

Smoke test **không phát hiện lỗi bảo mật mới**. Các rủi ro LOW/INFO đã được chấp nhận khi đóng Phase 1 vẫn còn nguyên, xem `README.md`, mục "Rủi ro đã chấp nhận khi đóng Phase 1". Ví dụ:
- `register` trả 409 làm lộ email đã đăng ký (A4 tái hiện đúng hành vi này).
- Cookie thiết bị quen nếu bị đánh cắp có thể dùng lại.
- Giới hạn IP chỉ đúng khi chạy một tiến trình.

## 12. Các vấn đề cần xử lý

| Mức | Vấn đề | Đề xuất |
|---|---|---|
| Dọn dẹp | `smoke_test.db` còn 3 tài khoản test (1 ADMIN có audit), 2 trusted device, 0 session hiệu lực | Xóa `smoke_test.db` sau khi bạn xác nhận (chưa xóa theo yêu cầu) |
| Dọn dẹp | `storage/dev-outbox/` còn 2 file có từ trước đợt test (không do test tạo; có thể chứa token reset của DB chính). File email do test tạo đã bị xóa sau khi dùng | Xóa các file cũ nếu không còn cần |
| LOW (đã biết) | `python -m app.server --help` crash trên console cp1258 | Đặt `PYTHONIOENCODING=utf-8` (đã ghi trong SKILL.md); có thể sửa ở phase sau |

Không có vấn đề nào chặn việc nghiệm thu Phase 1.

## 13. Kết luận tổng thể

**PASS – READY FOR PHASE 1 SIGN-OFF**

Đã xác minh trên server thật với database cô lập:
- Đăng ký, đăng nhập, session, CSRF, đăng xuất.
- Rate limit theo tài khoản, quên/đặt lại mật khẩu.
- Phân quyền USER/ADMIN, trusted device, audit.

Mọi ca chạy live đều đạt; database chính không bị tác động. Bốn ca không thể chạy live an toàn đã có test tự động bao phủ, hoặc thuộc chức năng chưa có trong Phase 1. Kết quả này là smoke test chức năng, **không thay thế security audit độc lập**.
