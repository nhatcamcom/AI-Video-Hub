# Kế hoạch Phase 2 — Video Creation UI

Trạng thái: **các quyết định ở mục 7 đã được chủ dự án xác nhận (2026-11-14); CLAUDE.md đã đồng bộ về worker; chưa triển khai code.**

## 1. Mục tiêu và phạm vi

Người dùng đã đăng nhập có thể: xem dashboard, tạo yêu cầu video từ prompt (kèm ảnh đầu vào tùy chọn), chọn tỷ lệ khung hình, thời lượng và model đã cấu hình, theo dõi trạng thái xử lý, xem thư viện, preview và tải video. Giao diện chạy tốt trên desktop và mobile.

**Không thuộc Phase 2:** tích hợp nhà cung cấp AI thật, credit, thanh toán, chia sẻ công khai, chỉnh sửa video. Mọi video được tạo bởi **MockProvider** và giao diện phải ghi rõ đây là bản demo — không giả vờ có AI thật.

## 2. Hiện trạng kế thừa từ Phase 0–1

- App factory `create_app()`, router dưới `/api/v1`, định dạng lỗi `{"error": {...}}`, `AppError` và các lớp con.
- Auth phía server bằng session cookie + CSRF: endpoint mới chỉ cần `CurrentUserDep` (đã kiểm tra CSRF cho POST/PATCH/DELETE). **Không xây lại đăng nhập.**
- Rate limit: in-memory theo IP/mạng (`enforce_rate_limits`) và theo tài khoản trong database (`AccountThrottle`, có thể dùng lại cho hạn mức theo `user_id`).
- Alembic với migration `0001`–`0003`; SQLite khi dev, chuẩn bị cho PostgreSQL.
- **Chưa có:** frontend (không có `static/`, `templates/`), phục vụ file tĩnh, upload (chưa có `python-multipart`), lưu trữ file, worker, header bảo mật cho HTML (CSP…).

## 3. Kiến trúc đề xuất

```text
app/
├── web/                    # phục vụ trang HTML tĩnh + header bảo mật (CSP, X-Frame-Options…)
├── core/storage.py         # LocalStorage: khóa do server sinh, chống path traversal, nằm ngoài static/
├── modules/
│   ├── media/              # ảnh đầu vào: models, schemas, service, router (upload, xem ảnh của mình)
│   └── videos/             # yêu cầu tạo video + kết quả: models, schemas, service, router
│       └── providers/      # VideoProvider (Protocol) + MockProvider + registry model đã cấu hình
├── worker.py               # `python -m app.worker` (quyết định 7.4): entrypoint mỏng — vòng lặp, tín hiệu dừng, log;
│                           #   logic chiếm job / gọi provider / cập nhật trạng thái nằm trong service của modules/videos/
static/
├── css/ (tokens.css, base.css, theo trang)
├── js/  (api.js, auth.js, create.js, library.js, status.js…)  # ES module, không build step
└── *.html (login, register, forgot, reset, dashboard, create, library, video)
storage/                    # đã git-ignore: uploads/, videos/, posters/
```

- **Frontend:** HTML/CSS/JS thuần do FastAPI phục vụ (StaticFiles của Starlette 1.10, không cần thêm dependency — đã kiểm tra import được trong `.venv`). Dữ liệu người dùng render bằng `textContent`. CSP chặt, không inline script.
- **Hàng đợi:** bảng `videos` đóng vai trò hàng đợi bền (cột `status`), worker chạy ở **tiến trình riêng** chiếm job bằng `UPDATE … WHERE status='queued'` có điều kiện (PostgreSQL: `FOR UPDATE SKIP LOCKED`). Không chạy tạo video trong request và **không chạy worker bên trong tiến trình web server**; server và worker chỉ giao tiếp qua database và thư mục lưu trữ, khởi động/dừng độc lập. Cùng thiết kế này dùng lại ở giai đoạn tích hợp AI thật.
- **MockProvider:** chuyển trạng thái `queued → processing → succeeded/failed` với tiến độ giả lập, sao chép file MP4 mẫu do chủ dự án cung cấp (quyết định 7.3) làm kết quả; đường dẫn file mẫu lấy từ cấu hình (tên đề xuất `MOCK_SAMPLE_VIDEO_PATH`). Thiếu file mẫu → job `failed` với `error_code` rõ ràng (vd. `mock_sample_missing`); không tự tải hay tự tạo video mẫu. Test tự động dùng file giả trong `tmp_path`, không cần file MP4 thật. Có thể cấu hình để thất bại có chủ đích (kiểm thử UI lỗi).
- **Poster (ảnh đại diện) là tùy chọn trong Phase 2:** chưa có ffmpeg để trích khung hình từ video, nên MockProvider không sinh poster; giao diện hiển thị placeholder và `GET /videos/{id}/poster` trả `404` khi không có.

## 4. Mô hình dữ liệu (migration mới, không sửa 0001–0003)

**`media_assets`** (migration 0004): `id`, `owner_id` FK users ON DELETE CASCADE (index), `storage_key` (unique, do server sinh), `content_type` (CHECK trong whitelist), `size_bytes` (số byte **thực ghi xuống đĩa**, tức là sau khi đã loại bỏ metadata), `width`, `height` (nullable — điền khi cách xử lý ảnh chọn ở P2-4 đọc được kích thước), `sha256`, `created_at`, `deleted_at` (nullable). Index `(owner_id, created_at)`.

**`user_storage_usage`** (migration 0004): `user_id` PK + FK users ON DELETE CASCADE, `used_bytes` BigInteger NOT NULL default 0 (CHECK `used_bytes >= 0`), `updated_at`. Một hàng mỗi user, là nguồn duy nhất để kiểm tra hạn mức (xem 4.1).

**`videos`** (migration 0005): `id`, `owner_id` FK CASCADE, `prompt` Text (giới hạn độ dài ở schema), `input_image_id` FK media_assets SET NULL (nullable, index), `aspect_ratio` (CHECK `16:9`/`9:16`/`1:1`), `duration_seconds` (CHECK trong tập cho phép), `model_key`, `status` (CHECK `queued/processing/succeeded/failed`), `progress` 0–100, `error_code` (nullable, mã an toàn để hiển thị), `output_storage_key`, `output_size_bytes` (nullable), `poster_storage_key` (nullable), `poster_size_bytes` (nullable), `created_at`, `started_at`, `finished_at`, `deleted_at`. Index `(owner_id, created_at)` cho thư viện, `(status, created_at)` cho worker.

### 4.1 Hạn mức lưu trữ (quyết định 7.5)

- **Hạn mức:** 1 GB/user = **1.073.741.824 byte**, đặt trong config (`STORAGE_QUOTA_BYTES_PER_USER`), kiểm tra **ở backend**; frontend chỉ hiển thị.
- **Tính vào dung lượng của user:** mọi file server lưu thay cho user — ảnh đầu vào đã upload (bản đã loại bỏ metadata), file video kết quả (kể cả bản sao do MockProvider tạo), poster nếu có. Tính theo **số byte thực ghi xuống đĩa**, không theo `Content-Length` hay kích thước client khai báo.
- **Không tính:** dữ liệu trong database, file tạm của upload đang diễn ra (đã bị giới hạn 10 MB mỗi upload), file mẫu của MockProvider trong `storage/` (thuộc hệ thống).
- **Chống vượt hạn mức khi upload đồng thời:** sau khi ghi xong file tạm và biết số byte thật, giữ chỗ bằng **một câu `UPDATE` có điều kiện** trên `user_storage_usage` (`used_bytes + :size <= :quota`) — cùng mẫu nguyên tử đã dùng cho `AccountThrottle` ở Phase 1. Thành công thì chuyển file vào vị trí chính và ghi bản ghi trong cùng transaction; thất bại thì xóa file tạm và trả **`409 storage_quota_exceeded`** (quyết định 7.6). Lỗi giữa chừng sau khi đã giữ chỗ thì hoàn lại số byte (compensating update) và xóa file.
- **Video kết quả:** worker giữ chỗ theo kích thước file thật trước khi ghi nhận kết quả; không đủ chỗ → job `failed` với `error_code=storage_quota_exceeded`, xóa file kết quả. `POST /videos` từ chối sớm khi user đã dùng hết hạn mức.
- **Giải phóng (quyết định 7.7):** hạn mức **chỉ được giải phóng sau khi đã xác nhận xóa file thành công**.
  - `DELETE /videos/{id}` đặt `deleted_at` (video biến mất khỏi thư viện ngay), sau đó thử xóa từng file (video kết quả, poster).
  - Xóa file thành công (hoặc file đã không còn trên đĩa) → trong một transaction: trừ `used_bytes` đúng bằng `size_bytes` đã ghi, xóa `*_storage_key` / `*_size_bytes` của file đó.
  - Xóa file thất bại → **giữ nguyên** `*_storage_key` và `*_size_bytes`, không trừ `used_bytes`; bản ghi (có `deleted_at` nhưng còn `storage_key`) là việc tồn đọng cho lệnh cleanup thử lại. Response vẫn là `204`.
  - Thứ tự luôn là xóa file trước, cập nhật DB sau: nếu tiến trình chết giữa hai bước thì `used_bytes` chỉ có thể bị tính **thừa** (an toàn), không bao giờ giải phóng hạn mức cho file vẫn còn trên đĩa; bước đối soát sẽ sửa phần thừa.
  - Cùng quy tắc áp dụng cho ảnh trong `media_assets` khi có thao tác xóa ảnh.
- **Cleanup và đối soát:** lệnh CLI (tên đề xuất `python -m app.cli cleanup-storage`) thử xóa lại file của bản ghi đã xóa mềm, dọn file tạm của upload dở dang, và đối soát `used_bytes` với tổng `size_bytes` của các file còn được tham chiếu.

## 5. API (dưới `/api/v1`, mọi endpoint yêu cầu đăng nhập trừ khi ghi khác)

| Method | Đường dẫn | Kết quả |
|---|---|---|
| GET | `/video-models` | Danh sách model đã cấu hình (Phase 2: chỉ `mock`, gắn nhãn demo) kèm tỷ lệ và thời lượng hỗ trợ |
| POST | `/media/images` | Multipart upload ảnh → `201` + `Location`; `413` quá 10 MB (đếm byte thật khi đọc); `415` sai định dạng (xác định bằng magic bytes; không tin `content_type` hay tên/đuôi file client gửi); `409 storage_quota_exceeded`; `429` |
| GET | `/media/images/{id}` | Ảnh của chính mình; của người khác → `404` (không lộ tồn tại) |
| POST | `/videos` | JSON `{prompt, aspect_ratio, duration_seconds, model_key, input_image_id?}` → `202` + `Location`; `422` khi tổ hợp không được model hỗ trợ; `409` khi vượt số job đang chạy cho phép; `429` |
| GET | `/videos?cursor=&limit=&status=` | Thư viện của mình, cursor pagination, `limit ≤ 50` |
| GET | `/videos/{id}` | Trạng thái, tiến độ, metadata |
| GET | `/videos/{id}/content` | Stream MP4 (hỗ trợ Range để tua); `?download=1` → `Content-Disposition: attachment` |
| GET | `/videos/{id}/poster` | Ảnh đại diện |
| DELETE | `/videos/{id}` | `204`; ẩn ngay (`deleted_at`), xóa file ngay; hạn mức chỉ giải phóng khi xóa file thành công, thất bại thì giữ thông tin dung lượng cho cleanup (4.1) |

Mọi truy cập tài nguyên kiểm tra quyền sở hữu ở tầng service; tài nguyên của người khác trả `404`.

## 6. Task và tiêu chí nghiệm thu

Mỗi task: TDD (RED → GREEN), ruff, mypy strict, pytest (coverage ≥ 80%, phần auth/upload/phân quyền 100%), bandit, pip-audit.

| # | Task | Tiêu chí nghiệm thu |
|---|---|---|
| P2-0 | Chốt quyết định mục 7, ghi ADR (hàng đợi DB + worker, frontend không build step) | Quyết định đã chốt; README và CLAUDE.md đã đồng bộ (2026-11-14). **Còn lại:** ADR trong `docs/adr/` |
| P2-1 | Khung web: phục vụ `static/`, layout chung, `tokens.css`, `api.js`, middleware header bảo mật | Xem chi tiết ở 6.1 |
| P2-2 | Trang đăng nhập / đăng ký / quên & đặt lại mật khẩu (UI cho API Phase 1) | Xem chi tiết ở 6.2 |
| P2-3 | `core/storage.py` (LocalStorage) + cấu hình giới hạn + hạn mức 1 GB/user (4.1) | Khóa ngẫu nhiên do server sinh; test chống path traversal; thư mục ngoài `static/` và đã git-ignore; giữ chỗ hạn mức nguyên tử, test nhiều upload đồng thời không vượt 1.073.741.824 byte |
| P2-4 | Upload ảnh + `media_assets` + `user_storage_usage` + migration 0004; cài `python-multipart`; **xem xét Pillow** để loại bỏ metadata (7.8) — mọi dependency kiểm tra tương thích trước khi cài | Định dạng xác định **chỉ bằng magic bytes** (whitelist PNG/JPEG/WebP); `content_type` và tên/đuôi file client gửi bị bỏ qua (test: file PNG gửi kèm `content_type=image/jpeg` và tên `x.exe` vẫn được lưu là PNG; file HTML gửi kèm `content_type=image/png` → 415); tên file và đuôi do server đặt theo định dạng đã xác định; phục vụ lại bằng content type đã lưu + `nosniff`; đếm byte thật, dừng ở 10 MB; 409/413/415/429; không tin kích thước client khai báo; **file lưu lâu dài không còn EXIF/GPS** (test bằng ảnh mẫu có EXIF/GPS cho cả JPEG, PNG, WebP); ảnh người khác → 404; migration up/down sạch |
| P2-5 | Registry model + `GET /video-models` | Chỉ trả model bật trong cấu hình; Mock gắn nhãn demo; test |
| P2-6 | `videos` + migration 0005 + `POST /videos` | Validate tỷ lệ/thời lượng theo khả năng của model; ảnh đầu vào phải thuộc người tạo; giới hạn job đang chạy mỗi user; `202` + `Location`; hạn mức tạo theo user (dùng lại `AccountThrottle`) |
| P2-7 | Worker + MockProvider (`python -m app.worker`, tiến trình riêng) | Hai worker không lấy trùng job (test đồng thời); job kẹt ở `processing` khi worker chết được xử lý lại theo thời hạn; chuyển trạng thái đúng; đường dẫn file mẫu cấu hình được, thiếu file mẫu → `failed` + thông báo rõ (test dùng file giả, không cần MP4 thật); lỗi provider → `failed` + `error_code` an toàn; không gọi API AI thật; server chạy được khi không có worker và ngược lại |
| P2-8 | API đọc: list (cursor), detail, content (Range, download), poster, delete + lệnh cleanup/đối soát lưu trữ | Chỉ chủ sở hữu; cursor ổn định; Range trả `206`; test IDOR; xóa file thành công → `used_bytes` giảm đúng; xóa file thất bại → `used_bytes` không đổi và cleanup xử lý lại được; đối soát sửa được sai lệch |
| P2-9 | UI Dashboard | Video gần đây, nút tạo mới; đủ trạng thái loading/empty/error |
| P2-10 | UI Tạo video | Prompt có bộ đếm ký tự; upload ảnh có preview và tiến độ; chọn tỷ lệ, thời lượng, model lấy từ API; chặn submit hai lần |
| P2-11 | Hiển thị trạng thái xử lý | Polling có backoff, dừng khi kết thúc; `aria-live`; tiến độ rõ ràng; lỗi kèm cách thử lại |
| P2-12 | UI Thư viện + trang chi tiết | Lưới thumbnail `aspect-ratio`, lọc theo trạng thái trên URL, phân trang cursor; preview (modal giữ focus, Esc đóng) và tải về |
| P2-13 | Responsive + QA | Kiểm tra 375 / 768 / 1440 px, bàn phím, tương phản ≥ 4.5:1, `prefers-reduced-motion`; E2E các luồng chính (Playwright chưa cài theo quyết định 7.2 — cần chủ dự án đồng ý trước khi dùng) |
| P2-14 | Verification, security review Phase 2, tài liệu | Toàn bộ công cụ đạt; review không còn HIGH/CRITICAL; README/CLAUDE.md cập nhật |

**Thứ tự:** P2-0 → P2-1 → P2-2 → P2-3 → P2-4 → P2-5 → P2-6 → P2-7 → P2-8 → P2-9…P2-12 (song song được sau khi API ổn định) → P2-13 → P2-14.

### 6.1 Chi tiết P2-1 — Khung web và header bảo mật

**Mục tiêu:** app phục vụ được trang HTML tĩnh và tài nguyên `static/` cùng origin với API, mọi response có header bảo mật, để các task giao diện sau dựng lên trên.

**Trong phạm vi:**
- Thư mục `static/` (`css/tokens.css`, `css/base.css`, `js/api.js`, một trang khung `index.html` có layout chung: header, vùng nội dung `main`, vùng thông báo `aria-live`).
- `app/web/`: mount `StaticFiles` cho `/static`, route trả trang khung tại `/`; không dùng Jinja2.
- `static/js/api.js`: hàm gọi API duy nhất (`credentials: "same-origin"`, tự gửi `X-CSRF-Token` lấy từ cookie CSRF hiện có cho POST/PATCH/PUT/DELETE, ánh xạ envelope `{"error": {...}}` thành lỗi có `code`/`status`).
- Middleware header bảo mật cho **mọi** response (API và static): `Content-Security-Policy` (`default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'`), `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: strict-origin-when-cross-origin`, `Permissions-Policy` tắt camera/micro/vị trí; `Strict-Transport-Security` chỉ khi `COOKIE_SECURE=true` (HTTPS).
- Ngoại lệ CSP có kiểm soát cho `/docs`, `/redoc`, `/docs/oauth2-redirect` (Swagger UI / ReDoc cần script CDN và inline), chỉ khi docs đang bật. Docs (gồm cả `/openapi.json`) **chỉ bật ở `development`**; `test`, `staging`, `production` trả `404` và không có ngoại lệ CSP nào (quyết định 7.9).
- Test và cập nhật README phần chạy giao diện.

**Ngoài phạm vi P2-1:** trang đăng nhập/đăng ký (P2-2), lưu trữ và hạn mức (P2-3), upload và `python-multipart` (P2-4), model, video, worker (P2-5 trở đi), mọi migration DB, mọi dependency mới.

**Tiêu chí nghiệm thu:**
1. `GET /` trả trang khung (`text/html`); `GET /static/css/tokens.css` và `GET /static/js/api.js` trả đúng content-type.
2. Mọi response (kể cả `GET /api/v1/health`, lỗi 404, lỗi 401) có đủ header bảo mật trên; CSP không chứa `unsafe-inline` hay `unsafe-eval`; HSTS chỉ có khi `COOKIE_SECURE=true`.
3. Path traversal qua `/static` (vd. `/static/../app/main.py`, dạng mã hóa `%2e%2e`) trả `404`; không truy cập được `storage/`, `.env` hay mã nguồn qua đường dẫn tĩnh.
4. Route `/api/v1/*` không bị static che; mọi test hiện có vẫn pass.
5. `/docs`, `/redoc`, `/docs/oauth2-redirect`, `/openapi.json` dùng được ở `development`; ở `test`, `staging`, `production` cả bốn trả `404` với CSP chặt (không có ngoại lệ docs).
6. Trang khung không có script hay style inline; không dùng `innerHTML`; có một `<h1>`, landmark `header`/`main`, focus nhìn thấy được.
7. ruff, mypy strict, pytest (coverage ≥ 80%), bandit, pip-audit đều đạt; có bằng chứng RED → GREEN.

**Dependency:** không cần dependency mới (StaticFiles có sẵn trong Starlette 1.10, không cần `aiofiles`). **Không cài Pillow trong P2-1**; Pillow chỉ được xem xét ở P2-4.

### 6.2 Chi tiết P2-2 — Giao diện xác thực (đã duyệt 2026-11-14)

**Mục tiêu:** người dùng đăng ký, đăng nhập, đăng xuất, quên và đặt lại mật khẩu qua giao diện, dùng **nguyên API và cơ chế session/CSRF của Phase 1** (không sửa backend auth).

**Quyết định của chủ dự án:**
- A. Đăng ký thành công → tự gọi `POST /auth/login`; nếu lỗi, báo tài khoản đã được tạo và hướng dẫn đăng nhập thủ công.
- B. Giữ `/forgot-password` và `/reset-password` trong P2-2 (link email Phase 1 trỏ cố định tới `/reset-password#token=...`).
- C. Chấp nhận `GET /auth/me` trả `401` khi chưa đăng nhập (Console ghi dòng 401); không sửa backend.
- D. Kiểm thử trình duyệt dùng DB tạm trong scratchpad: in và xác minh `DATABASE_URL` thực tế trước khi chạy Alembic, dừng nếu không xác minh được; email test ghi vào outbox tạm (`EMAIL_BACKEND=file`), chỉ dùng `@example.com`.

**API sử dụng** (`/api/v1`): `POST /auth/register`, `POST /auth/login`, `POST /auth/logout` (có `X-CSRF-Token`), `GET /auth/me`, `POST /auth/forgot-password`, `POST /auth/reset-password`.

**Trong phạm vi:** trang `/login`, `/register`, `/forgot-password`, `/reset-password` (danh sách trắng `PAGE_FILES`); header trạng thái phiên trên mọi trang (tên hiển thị bằng `textContent`, nút Đăng xuất); lỗi `422` hiện dưới từng ô, `429` hiện thời gian chờ, `403 csrf_failed` hướng dẫn tải lại trang, không tự gửi lại request khi gặp `401`/`403`.

**Ngoài phạm vi:** sửa backend auth/cookie/session/CSRF; trang hồ sơ (`PATCH /users/me`); xác minh email; dashboard (P2-9); chuyển hướng `?next=` (luôn về `/` để tránh open redirect).

**Tiêu chí nghiệm thu:**
1. 4 trang trả `200 text/html` với CSP chặt; đường dẫn ngoài danh sách (`/login.html`, `/admin`, `/Login`…) trả `404`; `POST` tới đường dẫn trang trả `405` và không phản hồi lại dữ liệu đã gửi.
2. Form `method="post"`, không `action`, mọi ô có `<label>`, `autocomplete` đúng (`current-password`/`new-password`/`email`/`username`); mật khẩu không bao giờ lên URL.
3. Đăng nhập sai → "Email hoặc mật khẩu không đúng", xóa ô mật khẩu; đúng → về `/`, header hiện tên.
4. Đăng ký: `422` theo ô, `409` theo ô email; thành công tự đăng nhập; tự đăng nhập lỗi → hướng dẫn đăng nhập thủ công.
5. Đăng xuất gửi `X-CSRF-Token`; header xóa tên cũ; sau đó `/auth/me` trả `401`.
6. Quên mật khẩu: cùng một thông báo cho email có và không có.
7. Đặt lại mật khẩu: token đọc từ fragment, xóa khỏi URL ngay (kể cả khi mở link lúc đang ở chính trang — `hashchange`), không lưu storage, không gọi API khi thiếu token hợp lệ; thành công → mọi phiên bị thu hồi, header về chưa đăng nhập; dùng lại token → báo hết hạn + link yêu cầu mới.
8. Kiểm thử trình duyệt ở 390x844 và 1440x900: không vi phạm CSP, không lỗi JS (lỗi mạng chỉ là phản hồi 4xx của `/api/v1`), vùng chạm ≥ 44px, focus nhìn thấy được.
9. pytest (coverage ≥ 80%), ruff, mypy, bandit, pip-audit đạt; có bằng chứng RED → GREEN; DB chính không đổi.

## 7. Quyết định đã được chủ dự án xác nhận (2026-11-14)

1. **Lộ trình:** Phase 2 là **Video Creation UI**, gồm thư viện video, giao diện tạo video và MockProvider. Phase 4 chỉ tập trung tích hợp các nhà cung cấp AI video thực tế. README đã cập nhật theo lộ trình này.
2. **Dependency:** `python-multipart` là dependency bắt buộc cho upload ảnh (cài ở P2-4). **Chưa cài Pillow** — Pillow được đưa vào phạm vi xem xét ở P2-4 (xem 7.8), không cài ở P2-1. **Chưa cài Playwright.** Chỉ cài dependency khi task thực sự cần và phải kiểm tra tương thích trước (Smart App Control trên máy dev chặn file biên dịch chưa ký).
3. **Video mẫu MockProvider:** một file MP4 ngắn do chủ dự án cung cấp và có quyền sử dụng, lưu trong `storage/` (không commit), **đường dẫn cấu hình được**. Nếu file chưa tồn tại, job thất bại với thông báo rõ ràng. Không tự tải hoặc tự tạo video mẫu thay chủ dự án. **Không yêu cầu cung cấp file MP4 trước P2-7.**
4. **Worker:** bắt đầu từ Phase 2; tiến trình riêng `python -m app.worker`; không chạy worker bên trong tiến trình web server; server và worker hoạt động độc lập. Cấu trúc: `app/worker.py` là entrypoint mỏng (cùng mẫu `app/server.py`, `app/cli.py`), nghiệp vụ job nằm trong service của `app/modules/videos/`; nếu entrypoint lớn hơn một file thì đổi thành package `app/worker/` có `__main__.py`, lệnh chạy không đổi. CLAUDE.md mục 0 và mục 3 đã được đồng bộ (bỏ "từ giai đoạn 4" và thư mục gốc `worker/`).
5. **Giới hạn mặc định:** ảnh tải lên tối đa 10 MB; tỷ lệ 16:9, 9:16, 1:1; thời lượng 5 hoặc 10 giây; tối đa 2 job đang chạy mỗi user; prompt tối đa 1.000 ký tự; dung lượng lưu trữ **1 GB/user (1.073.741.824 byte)**. Yêu cầu kèm theo: kiểm tra hạn mức ở backend; tính đến upload đồng thời để không vượt hạn mức do race condition; xác định rõ loại file được tính; không tin kích thước file do client khai báo. Cách đáp ứng ghi ở mục 4.1.
6. **Mã lỗi vượt hạn mức lưu trữ:** `409 storage_quota_exceeded` (envelope `{"error": {"code": "storage_quota_exceeded", ...}}`). Worker dùng cùng mã làm `error_code` khi job thất bại vì hết hạn mức.
7. **Xóa video và hạn mức:** chỉ giải phóng hạn mức sau khi đã xác nhận xóa file thành công. Nếu xóa file thất bại thì giữ lại thông tin dung lượng và đưa vào quy trình cleanup/đối soát. Chi tiết ở 4.1.
8. **Metadata ảnh:** ảnh upload phải được loại bỏ metadata EXIF/GPS **trước khi lưu trữ lâu dài** hoặc cung cấp cho bất kỳ ai khác ngoài người tải lên (kể cả gửi cho nhà cung cấp AI ở Phase 4). File gốc chỉ tồn tại dưới dạng file tạm trong lúc xử lý upload. Cách loại bỏ (Pillow hay cách khác) được quyết định ở P2-4.
9. **Tài liệu API theo môi trường (xác nhận khi nghiệm thu P2-1):** `/docs`, `/redoc`, `/docs/oauth2-redirect` và `/openapi.json` **chỉ bật khi `APP_ENV=development`**. Môi trường `test`, `staging`, `production` đều trả `404` cho cả bốn đường dẫn và không áp dụng ngoại lệ CSP của trang docs. Điều kiện dựa trên trường `app_env` có sẵn của `Settings`, không thêm cơ chế nhận diện môi trường mới.

## 8. Rủi ro và lưu ý bảo mật

- Upload là bề mặt tấn công mới: định dạng xác định bằng magic bytes (không tin `content_type`/tên file client), giới hạn kích thước khi đọc, tên file và đuôi do server đặt, phục vụ qua endpoint có kiểm tra quyền (không qua `static/`), `nosniff`.
- Serving HTML lần đầu → bắt buộc CSP và các header bảo mật (đã ghi nhận chưa có ở Phase 1).
- IDOR: mọi truy vấn lọc theo `owner_id`; test truy cập chéo tài khoản.
- Endpoint cần đăng nhập vẫn chưa có rate limit chung — Phase 2 thêm hạn mức cho upload và tạo video theo user.
- Các điều kiện triển khai của Phase 1 (khóa throttle, một process cho giới hạn IP, reverse proxy, PostgreSQL chưa kiểm thử thật) vẫn giữ nguyên.
- **Loại bỏ EXIF/GPS (7.8) và Pillow:** Pillow có phần biên dịch (`.pyd`), không có wheel thuần Python, nên có thể bị Smart App Control chặn trên máy dev — phải kiểm tra import trong `.venv` trước khi chọn. Nếu không dùng được Pillow, phương án thay thế là tự loại bỏ các đoạn metadata (JPEG APP1/APP13, PNG `eXIf`/`tEXt`/`iTXt`/`zTXt`, WebP `EXIF`/`XMP`) — không thêm dependency nhưng là code phân tích định dạng nhị phân cần test kỹ. Lựa chọn trình chủ dự án duyệt ở P2-4. Tái mã hóa ảnh (nếu dùng Pillow) làm đổi kích thước file nên hạn mức tính theo file sau xử lý.
- **Xóa tài khoản user:** FK `ON DELETE CASCADE` xóa bản ghi `media_assets`/`videos` nhưng không xóa file trên đĩa → file mồ côi không còn bản ghi để cleanup. Phase 1 chưa có thao tác xóa user; khi thêm, phải đi qua service xóa file trước, hoặc cleanup phải quét thư mục lưu trữ tìm khóa không còn được tham chiếu.
- **Server và worker cùng ghi một file SQLite** từ hai tiến trình: có thể gặp `database is locked` khi tải cao. Giữ transaction ngắn, đặt `busy_timeout`; cân nhắc chế độ WAL (file `*.db-wal`/`*.db-shm` đã được git ignore).
- **Worker chết giữa chừng:** job kẹt ở `processing` → cần thời hạn xử lý (lease) và cơ chế đưa lại hàng đợi hoặc đánh dấu `failed`; worker dừng an toàn khi nhận tín hiệu dừng; log không chứa prompt hay dữ liệu cá nhân.
- **Hạn mức lưu trữ:** sai lệch `used_bytes` nếu tiến trình chết giữa lúc giữ chỗ và ghi file, hoặc giữa lúc xóa file và cập nhật DB → cần bước đối soát định kỳ; file tạm của upload dở dang phải được dọn. Xóa file thất bại kéo dài (file bị khóa trên Windows, quyền thư mục) làm user bị tính dung lượng cho video đã xóa cho tới khi cleanup thành công — cần log cảnh báo (không ghi đường dẫn tuyệt đối hay dữ liệu cá nhân).
- **Đĩa đầy toàn hệ thống** không được hạn mức theo user bảo vệ (nhiều user × 1 GB): cần theo dõi dung lượng đĩa khi triển khai thật.
- **Rule rate limit IP mới** cho upload/tạo video phải có tên riêng và được thêm vào `AUTH_CAPACITY_SHARES` (quy ước CLAUDE.md); hiện tổng tỉ trọng đã là 100, nên cần chia lại.
- **CSP và Swagger UI:** `/docs` cần script ngoài và inline; ngoại lệ chỉ áp dụng cho đúng `/docs`, `/redoc`, `/docs/oauth2-redirect` khi `APP_ENV=development`, không được nới CSP cho toàn app. Trang docs phụ thuộc CDN bên ngoài (jsdelivr, Google Fonts) nên cần internet khi xem.
