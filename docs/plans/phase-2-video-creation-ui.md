# Kế hoạch Phase 2 — Video Creation UI

Trạng thái: **các quyết định ở mục 7 đã được chủ dự án xác nhận (2026-11-14). Đã nghiệm thu P2-1 đến P2-6 và P2-4.1; P2-7 đã triển khai (mục 6.7), chờ nghiệm thu; P2-8 trở đi chưa triển khai.** Trạng thái chi tiết từng bước ở mục 6.1–6.7 và 6.4.1. Mục 2 mô tả hiện trạng TRƯỚC khi bắt đầu Phase 2.

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
│       ├── catalog.py      # P2-5 · danh mục model đã cấu hình (VideoModelSpec, ENABLED_VIDEO_MODELS)
│       └── providers/      # P2-7 · VideoProvider (Protocol) + MockProvider
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

## 4. Mô hình dữ liệu (migration mới, không sửa 0001–0004)

**`media_assets`** (migration 0005, P2-4): `id`, `owner_id` FK users ON DELETE CASCADE (index), `storage_key` (unique, do server sinh), `content_type` (CHECK trong whitelist), `size_bytes` (số byte **thực ghi xuống đĩa**, tức là sau khi đã loại bỏ metadata), `width`, `height` (NOT NULL, CHECK > 0 — quyết định 6.4.8), `sha256`, `created_at`, `deleted_at` (nullable). Index `(owner_id, created_at)`.

**`user_storage_usage`** (migration 0004, đã làm ở P2-3): `user_id` PK + FK users ON DELETE CASCADE, `used_bytes` BigInteger NOT NULL default 0 (CHECK `used_bytes >= 0`), `updated_at`. Một hàng mỗi user, là nguồn duy nhất để kiểm tra hạn mức (xem 4.1).

**`videos`** (migration 0006, P2-6): `id`, `owner_id` FK CASCADE, `prompt` Text (giới hạn độ dài ở schema), `input_image_id` FK media_assets SET NULL (nullable, index), `aspect_ratio` (CHECK `16:9`/`9:16`/`1:1`), `duration_seconds` (CHECK trong tập cho phép), `model_key`, `status` (CHECK `queued/processing/succeeded/failed`), `progress` 0–100, `error_code` (nullable, mã an toàn để hiển thị), `output_storage_key`, `output_size_bytes` (nullable), `poster_storage_key` (nullable), `poster_size_bytes` (nullable), `created_at`, `started_at`, `finished_at`, `deleted_at`. Index `(owner_id, created_at)` cho thư viện, `(status, created_at)` cho worker.

### 4.1 Hạn mức lưu trữ (quyết định 7.5)

- **Hạn mức:** 1 GB/user = **1.073.741.824 byte**, đặt trong config (`STORAGE_QUOTA_BYTES_PER_USER`), kiểm tra **ở backend**; frontend chỉ hiển thị.
- **Tính vào dung lượng của user:** mọi file server lưu thay cho user — ảnh đầu vào đã upload (bản đã loại bỏ metadata), file video kết quả (kể cả bản sao do MockProvider tạo), poster nếu có. Tính theo **số byte thực ghi xuống đĩa**, không theo `Content-Length` hay kích thước client khai báo.
- **Không tính:** dữ liệu trong database, file tạm của upload đang diễn ra (đã bị giới hạn 10 MB mỗi upload), file mẫu của MockProvider trong `storage/` (thuộc hệ thống).
- **Chống vượt hạn mức khi upload đồng thời:** sau khi ghi xong file tạm và biết số byte thật, giữ chỗ bằng **một câu `UPDATE` có điều kiện** trên `user_storage_usage` (`used_bytes + :size <= :quota`) — cùng mẫu nguyên tử đã dùng cho `AccountThrottle` ở Phase 1. Giữ chỗ tự commit một transaction ngắn; việc chuyển file vào vị trí chính và ghi bản ghi diễn ra SAU đó, không giữ transaction trong lúc làm I/O file (chi tiết ở 6.3). Không đủ chỗ thì xóa file tạm và trả **`409 storage_quota_exceeded`** (quyết định 7.6). Lỗi giữa chừng sau khi đã giữ chỗ thì hoàn lại số byte (compensating update) và xóa file.
- **Video kết quả:** worker giữ chỗ theo kích thước file thật trước khi ghi nhận kết quả; không đủ chỗ → job `failed` với `error_code=storage_quota_exceeded`, xóa file kết quả. `POST /videos` từ chối sớm khi user đã dùng hết hạn mức.
- **Giải phóng (quyết định 7.7):** hạn mức **chỉ được giải phóng sau khi đã xác nhận xóa file thành công**.
  - `DELETE /videos/{id}` đặt `deleted_at` (video biến mất khỏi thư viện ngay), sau đó thử xóa từng file (video kết quả, poster).
  - Xóa file thành công (hoặc file đã không còn trên đĩa) → trong một transaction: trừ `used_bytes` đúng bằng `size_bytes` đã ghi, xóa `*_storage_key` / `*_size_bytes` của file đó.
  - Xóa file thất bại → **giữ nguyên** `*_storage_key` và `*_size_bytes`, không trừ `used_bytes`; bản ghi (có `deleted_at` nhưng còn `storage_key`) là việc tồn đọng cho lệnh cleanup thử lại. Response vẫn là `204`.
  - Thứ tự luôn là xóa file trước, cập nhật DB sau: nếu tiến trình chết giữa hai bước thì `used_bytes` chỉ có thể bị tính **thừa** (an toàn), không bao giờ giải phóng hạn mức cho file vẫn còn trên đĩa; bước đối soát sẽ sửa phần thừa.
  - Cùng quy tắc áp dụng cho ảnh trong `media_assets` khi có thao tác xóa ảnh.
- **Cleanup và đối soát:** lệnh CLI (tên đề xuất `python -m app.cli cleanup-storage`) thử xóa lại file của bản ghi đã xóa mềm, dọn file tạm của upload dở dang, và đối soát `used_bytes` với tổng `size_bytes` của các file còn được tham chiếu.

### 4.2 Hợp đồng quota khi ghi file kết quả (P2-7) và khi xóa / đối soát (P2-8) — ĐÃ DUYỆT (2026-11-15), áp dụng khi triển khai P2-7/P2-8

**P2-6 không giữ chỗ và không hoàn lại quota** (quyết định D1): `POST /videos` chỉ kiểm tra sớm `usage >= quota` → `409 storage_quota_exceeded`. Mọi thao tác `reserve`/`release` cho video nằm ở P2-7/P2-8 theo hợp đồng dưới đây.

**Bất biến:**
1. Chỉ `release` khi đã **chắc chắn** file mà lượt giữ chỗ đại diện (file tại `storage_key` ĐÍCH) không còn trên đĩa **và** không có bản ghi đã commit nào tham chiếu tới nó. File tạm trong `tmp/` không tính hạn mức (4.1).
2. Không xác định chắc chắn được → **giữ nguyên file và quota**, ghi log khóa tương đối + "cần đối soát" (không ghi đường dẫn tuyệt đối, prompt hay dữ liệu cá nhân); P2-8 xử lý. Mọi sai lệch chỉ được phép theo hướng tính THỪA.
3. Mỗi lượt giữ chỗ được trả **tối đa một lần**, ở đúng một nhánh code (cờ cục bộ `reserved_bytes`, đặt về 0 ngay sau khi `release` trả về).
4. `asyncio.CancelledError` luôn được ném lại sau khi xử lý; phần dọn dẹp chạy trong `asyncio.shield` để một lần hủy nữa không cắt ngang giữa chừng.

**Luồng ghi file kết quả của worker (P2-7)**, dùng lại 4 bước của P2-3:

| Bước | Sự cố | File đích | Xử lý file | Quota | Job |
|---|---|---|---|---|---|
| 1. `write_temp_file` | lỗi / bị hủy | chưa có | `LocalStorage` tự xóa file dở | chưa giữ chỗ | `failed` (bị hủy: để lease xử lý) |
| 2. `reserve(owner, byte thật)` | `StorageQuotaExceededError` | chưa có | `discard_temp_file` | không giữ được | `failed`, `error_code=storage_quota_exceeded` |
| 2. `reserve` | bị hủy / lỗi khác | chưa có | `discard_temp_file` | **không xác định** (UPDATE có thể đã commit) → không `release`, P2-8 đối soát | để lease xử lý |
| 3. `commit_temp_file` | xem "Hủy trong lúc đổi chỗ file" | | | | |
| 4. Ghi kết quả vào `videos` | xem "Kết quả UPDATE/COMMIT không xác định" | | | | |

**Hủy trong lúc đổi chỗ file (bước 3).** `os.replace` chạy trong thread (`asyncio.to_thread`); hủy coroutine KHÔNG dừng thread, nên bị hủy **không có nghĩa** file chưa được ghi. P2-7 cần một thao tác đổi chỗ trả về kết quả rõ ràng (`COMMITTED` / `NOT_COMMITTED` / `UNKNOWN`). Việc này thay đổi `app/core/storage.py`, phải được duyệt riêng ở P2-7:
- Giữ tham chiếu tới future của thread và `await asyncio.shield(future)`.
- **Hủy TRƯỚC khi thread `os.replace` được tạo** (ví dụ đang `mkdir`): chắc chắn chưa đổi chỗ → `NOT_COMMITTED`.
- **Hủy TRONG lúc thread chạy:** chờ thread kết thúc (vẫn trong `shield`), rồi xét: `os.replace` ném lỗi → `NOT_COMMITTED`; trả về bình thường → `COMMITTED`. Bị hủy lần nữa khi đang chờ → `UNKNOWN`.
- Sau đó luôn kiểm tra lại file đích trên đĩa (`path_for(key)`, `stat` không theo liên kết): `NOT_COMMITTED` mà file đích vẫn tồn tại, hoặc `stat` lỗi → nâng thành `UNKNOWN`.
- Xử lý: `NOT_COMMITTED` + file đích không tồn tại → `release` một lần, `discard_temp_file`. `COMMITTED` → **tuyệt đối không `release`**; nếu coroutine đang bị hủy thì không ghi tiếp DB, giữ file + quota (thành orphan nếu không có bản ghi; P2-8 dọn). `UNKNOWN` → giữ file + quota, log "cần đối soát".
- **Hủy SAU khi `os.replace` xong** (đang ở bước 4): file đã ở đích → theo quy tắc bước 4, không bao giờ `release` trước khi xác định chắc chắn.

**Kết quả UPDATE/COMMIT không xác định (bước 4).** Ghi kết quả là CAS `UPDATE videos SET output_storage_key=:key, output_size_bytes=:size, status='succeeded', progress=100, active_slot=NULL, finished_at=:now WHERE id=:id AND status='processing'` (P2-7 bổ sung điều kiện lease) rồi `commit`. `execute` và `commit` đặt trong hai khối riêng để biết lỗi xảy ra ở đâu:
- Lỗi khi `execute` (trước khi gửi COMMIT) → transaction chắc chắn không được commit → `rollback`, rồi **kiểm tra lại bằng session MỚI** (kết nối mới, transaction mới, chỉ đọc `status, output_storage_key` của video).
- `rowcount == 0` (mất lease / video đã bị xóa) → `rollback`, kiểm tra lại bằng session mới.
- Lỗi hoặc bị hủy khi `commit` → **kết quả không xác định** (COMMIT có thể đã thành công phía server) → `rollback` session cũ, kiểm tra lại bằng session mới.
- Quy tắc sau khi kiểm tra lại:
  - Video tham chiếu đúng `:key` (đã `succeeded`) → commit thực ra đã thành công → **giữ file và quota**, coi là thành công.
  - Video KHÔNG tham chiếu `:key` và lỗi chắc chắn xảy ra trước COMMIT (lỗi `execute` hoặc `rowcount == 0`) → `storage.delete(key)`: `DELETED`/`NOT_FOUND` → `release` một lần; `FAILED` → giữ quota, log "cần đối soát".
  - Video KHÔNG tham chiếu `:key` nhưng lỗi xảy ra ở COMMIT → vẫn coi là **không xác định** (server có thể chưa xử lý xong COMMIT cũ) → giữ file + quota, log "cần đối soát"; P2-8 quyết định sau thời gian an toàn.
  - Không kiểm tra lại được (DB lỗi, bị hủy) → giữ file + quota.
- Trạng thái job khi không xác định: worker không tự đổi; lease hết hạn (P2-7) xử lý tiếp, và cơ chế lease KHÔNG được xóa kết quả của job đã `succeeded`.

**Xóa video (P2-8), không trả hai lần.** Xóa file trước (quyết định 7.7). `DELETED`/`NOT_FOUND` → CAS `UPDATE videos SET output_storage_key=NULL, output_size_bytes=NULL WHERE id=:id AND output_storage_key=:key`, rồi `StorageQuotaService(db).release(...)` trên CÙNG session (`release` tự commit → hai thay đổi commit nguyên tử). Chỉ request có `rowcount == 1` mới trả quota; hai request xóa đồng thời → một lần `release`. `FAILED` → không đổi DB, không trả quota (cleanup thử lại). Cùng quy tắc cho poster.

**Đối soát và orphan file (P2-8, `cleanup-storage`):**
- Dọn `tmp/` bằng `remove_stale_temp_files` (đã có, file quá 1 giờ).
- Orphan trong `videos/`, `posters/`, `uploads/`: khóa không được bản ghi nào tham chiếu **và** cũ hơn thời gian an toàn (lớn hơn thời hạn lease của P2-7 + biên) → xóa. File còn mới thì để lại (có thể là commit chưa xác định).
- Đối soát `used_bytes` = tổng `size_bytes` của file còn được tham chiếu và còn tồn tại (`media_assets` chưa xóa, `videos.output_*`, `videos.poster_*`), ghi bằng `StorageQuotaService.set_used()`. Bỏ qua user đang có job `processing` để không trừ phần worker vừa giữ chỗ nhưng chưa ghi vào `videos`.

**Test bắt buộc ở P2-7/P2-8** (đếm số lần gọi `release` và `storage.delete` bằng spy trong mọi trường hợp):
- Hủy TRƯỚC `os.replace` (chặn `mkdir` bằng `threading.Event`, hủy task) → `release` đúng 1 lần, file tạm bị xóa, `CancelledError` được ném lại.
- Hủy TRONG `os.replace` (chặn `os.replace` trong thread, hủy task, rồi cho thread chạy tiếp và đổi chỗ thành công) → file đích tồn tại, **`release` 0 lần**, file được giữ.
- Hủy TRONG `os.replace`, thread sau đó ném lỗi → file đích không có → `release` đúng 1 lần.
- Hủy lần hai khi đang chờ thread → `UNKNOWN` → `release` 0 lần, log "cần đối soát".
- `stat` file đích lỗi → `release` 0 lần.
- Hủy SAU `os.replace` (đang ghi DB) → `release` 0 lần, file được giữ.
- **Commit thật sự thành công nhưng worker nhận exception** (bọc `commit` thật rồi ném `ConnectionError`) → session mới thấy video `succeeded` tham chiếu `:key` → `storage.delete` 0 lần, `release` 0 lần, file còn và đọc được.
- Commit ném lỗi mà không commit → giữ file + quota (không xác định), log "cần đối soát".
- `execute` lỗi trước COMMIT, hoặc `rowcount == 0` → `delete` 1 lần, `release` 1 lần; `delete` trả `FAILED` → `release` 0 lần.
- Kiểm tra lại bằng session mới cũng lỗi → giữ file + quota.
- Xóa video đồng thời hai lần → `release` đúng 1 lần.
- Orphan cũ hơn thời gian an toàn bị xóa và đối soát đúng; orphan còn mới và file đang được tham chiếu không bị xóa.

**Phát hiện trên code P2-4 đã nghiệm thu → ĐÃ SỬA ở P2-4.1 (mục 6.4.1):** `MediaService._store_image` từng (1) hoàn hạn mức ngay khi `commit_temp_file` bị hủy trong lúc `os.replace` còn chạy (tính THIẾU + file mồ côi) và (2) xóa file + hoàn hạn mức khi COMMIT `media_assets` lỗi dù COMMIT có thể đã thành công. Cả hai đã được tái hiện bằng test (RED) rồi sửa theo hợp đồng này.

## 5. API (dưới `/api/v1`, mọi endpoint yêu cầu đăng nhập trừ khi ghi khác)

| Method | Đường dẫn | Kết quả |
|---|---|---|
| GET | `/video-models` | Danh sách model đã cấu hình (Phase 2: chỉ `mock`, gắn nhãn demo) kèm tỷ lệ và thời lượng hỗ trợ |
| POST | `/media/images` | Multipart upload ảnh → `201` + `Location`; `413` quá 10 MB (đếm byte thật khi đọc); `415` sai định dạng (xác định bằng magic bytes; không tin `content_type` hay tên/đuôi file client gửi); `409 storage_quota_exceeded`; `429` |
| GET | `/media/images/{id}` | Metadata ảnh của chính mình (JSON, không có `storage_key`); của người khác → `404` (không lộ tồn tại) |
| GET | `/media/images/{id}/content` | Nội dung ảnh (content type đã lưu, `nosniff`, `Cache-Control: private, no-store`); của người khác → `404` |
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
| P2-3 | `core/storage.py` (LocalStorage) + `storage_root` / hạn mức 1 GB/user (4.1) + `user_storage_usage` + migration 0004 | Xem chi tiết ở 6.3 |
| P2-4 | Upload ảnh (`POST /media/images`, `GET /media/images/{id}`, `GET /media/images/{id}/content`) + `media_assets` + migration 0005; Pillow 13.0.0 + python-multipart 0.0.33 | Xem chi tiết ở 6.4 |
| P2-5 | Danh mục model (`catalog.py`) + `GET /video-models` | Xem chi tiết ở 6.5 |
| P2-6 | `videos` + migration 0006 + `POST /videos` + `GET /videos/{id}` | Xem chi tiết ở 6.6 |
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

### 6.3 Chi tiết P2-3 — Lưu trữ file và hạn mức (đã duyệt 2026-11-14)

**Quyết định của chủ dự án:**
1. Bảng `user_storage_usage` và migration `0004` chuyển sang P2-3; `media_assets` là `0005` (P2-4), `videos` là `0006` (P2-6).
2. Được sửa `app/core/config.py`, tạo `app/core/storage.py`, sửa `app/model_registry.py`, thêm module quota và migration.
3. Module hạn mức: `app/modules/quota/`.
4. `STORAGE_ROOT` mặc định `storage`, gồm `uploads/`, `videos/`, `posters/`, `tmp/`; `dev-outbox/` không bị ảnh hưởng.
5. Giới hạn ảnh ở P2-4 là 10 MiB = 10.485.760 byte; P2-3 chỉ thêm `storage_root` và `storage_quota_bytes_per_user`.
6. Chưa thêm API xem dung lượng (để P2-9/P2-10).
7. `release` vượt số dư: không đưa về 0; UPDATE có điều kiện `used_bytes >= n`, không đủ thì giữ nguyên, ghi cảnh báo, trả kết quả không thành công để đối soát.
8. Hàm dọn file tạm quá hạn (mặc định 1 giờ) ở P2-3; CLI `cleanup-storage` ở P2-8.

**Đã xây (nền móng cho P2-4, P2-7, P2-8 — chưa có endpoint, chưa có giao diện):**
- `Settings.storage_root` / `storage_root_path`: đường dẫn tương đối tính từ thư mục dự án, resolve (cả symlink/junction) trước khi so sánh; từ chối thư mục nằm trong, trùng hoặc bao trùm `static/` / `app/`; không tạo thư mục khi đọc cấu hình. `storage_quota_bytes_per_user` > 0, mặc định 1.073.741.824.
- `app/core/storage.py` — `LocalStorage`: khóa `<kind>/<32 hex><đuôi>` sinh bằng `secrets` (kind/đuôi theo danh sách cho phép); `path_for` kiểm tra regex rồi kiểm tra đường dẫn sau resolve (chặn `..`, `\`, đường dẫn tuyệt đối, byte null, `tmp/`, `dev-outbox/`, symlink/junction thoát ra ngoài); `write_temp_file` → `commit_temp_file` (`os.replace` trong cùng thư mục gốc) / `discard_temp_file`; `delete` trả `DELETED` / `NOT_FOUND` / `FAILED`; `remove_stale_temp_files` chỉ xóa file thường quá hạn ngay trong `tmp/`, không theo liên kết. Mọi I/O đĩa qua `asyncio.to_thread`.
- `app/modules/quota/` — `UserStorageUsage` (migration `0004`) và `StorageQuotaService`: `reserve` (UPDATE có điều kiện `used_bytes + n <= quota`, kiểm `rowcount`, tạo hàng đầu tiên an toàn khi hai request cùng chèn), `release` (quyết định 7), `usage`, `set_used` (chỉ cho đối soát). Mỗi thao tác tự commit một transaction ngắn; `409 storage_quota_exceeded`.

**Luồng ghi file dùng ở P2-4/P2-7** (không giữ transaction trong lúc làm I/O file):
1. `write_temp_file` (không transaction) → biết số byte thật.
2. `reserve` (transaction ngắn, tự commit) → không đủ chỗ: `discard_temp_file`, trả `409`.
3. `commit_temp_file` (không transaction) → lỗi: `release` + `discard_temp_file`.
4. Ghi bản ghi (`media_assets`/`videos`) trong transaction ngắn riêng → lỗi: xóa file + `release`.

**Tiêu chí nghiệm thu (đã đạt):** khóa do server sinh; path traversal / symlink / kind-đuôi sai bị từ chối; `STORAGE_ROOT` không nằm trong / bao trùm `static/`, `app/`; giữ chỗ đúng hạn mức, vượt 1 byte → `409`; nhiều user độc lập; 20 request đồng thời trên file SQLite thật không vượt 1.073.741.824 byte; hoàn lại không âm; lỗi ghi/đổi chỗ/xóa xử lý đúng; migration `0004` lên/xuống sạch trên DB tạm (`down_revision = 0003`, downgrade chỉ xóa `user_storage_usage`); pytest/ruff/mypy/bandit/pip-audit đạt; `ai_video_hub.db` không đổi.

### 6.4 Chi tiết P2-4 — Upload ảnh (kế hoạch và giới hạn pixel đã duyệt 2026-11-15; đã triển khai và nghiệm thu 2026-11-15)

**Quyết định của chủ dự án:**
1. Pillow: thử trong venv tạm ngoài dự án trước (kiểm `PIL._imaging` và WebP); bị Smart App Control chặn thì dừng, không tự dùng phương án Python thuần.
2. Nhận file bằng `UploadFile` của FastAPI; chấp nhận file tạm của Starlette trong `%TEMP%`, bảo đảm đóng và dọn khi request kết thúc.
3. Giới hạn body toàn app khoảng 11 MiB (gồm overhead multipart) bằng `RequestBodyLimitMiddleware` của Starlette. Được duyệt thêm một middleware nhỏ (`PayloadTooLargeEnvelopeMiddleware`) để MỌI `413` trả envelope JSON `payload_too_large` và giữ header bảo mật; không đổi cách xử lý các mã lỗi khác.
4. Mã hóa lại: JPEG/WebP quality 90, PNG lossless; giữ ICC; loại EXIF, GPS, XMP, text metadata; áp hướng xoay trước khi lưu; từ chối ảnh động; ảnh sau xử lý tối đa 10 MiB (10.485.760 byte).
5. Giới hạn pixel (đã duyệt): JPEG/PNG ≤ 24 MP, WebP ≤ 8 MP, kiểm tra từ header trước khi giải mã và trước khi xin slot; giữ `Image.MAX_IMAGE_PIXELS` làm lớp bảo vệ thêm. Tối đa 2 ảnh xử lý đồng thời (thread pool riêng 2 worker), tối đa 4 request chờ, chờ quá 30 giây → `503 image_processing_busy` kèm `Retry-After`; request đang chờ không giải mã, không giữ cả file trong RAM.
6. API: `POST /media/images`, `GET /media/images/{id}` (metadata), `GET /media/images/{id}/content` (nội dung).
7. Rate limit: 60 lượt/IP/giờ, 30 lượt/user/giờ; rule media thêm vào phân bổ sức chứa chung (gộp ở `app/main.py`), không sửa module auth; kiểm thử cả rule media và rule auth hiện có.
8. `width`, `height` NOT NULL.
9. Được sửa `app/core/config.py`, `app/core/errors.py`, `app/core/dependencies.py`, `app/main.py`; không sửa auth/users/audit/health.
10. Chưa có API xóa ảnh trong P2-4.

**Kết quả kiểm tra trước khi triển khai (2026-11-15, venv tạm trong scratchpad, `.venv` dự án không đổi):**
- **Pillow 13.0.0** (`cp312-win_amd64`): `PIL._imaging`, `PIL._webp`, `PIL._imagingcms` import được dù Smart App Control đang bật và 8 file `.pyd` không có chữ ký số; hỗ trợ WebP, JPEG, zlib, littlecms2 (ICC). Mã hóa lại JPEG/PNG/WebP (có EXIF + GPS + XMP + text + ICC) → kiểm tra theo cấu trúc file: không còn APP1(Exif/XMP)/COM (JPEG), `eXIf`/`tEXt`/`iTXt` (PNG), `EXIF`/`XMP` (WebP); ICC được giữ; hướng xoay đúng; ảnh động WebP/PNG bị phát hiện; decompression bomb bị chặn ngay khi `open()`.
- **python-multipart 0.0.33**: wheel thuần Python (`py3-none-any`). pip-audit: Pillow 13.0.0 và python-multipart 0.0.33 không có lỗ hổng đã biết.
- **Starlette 1.10.0 / FastAPI 0.162.5**: FastAPI KHÔNG chuyển tham số `max_body_size` của Starlette, nên phải `add_middleware(RequestBodyLimitMiddleware, ...)`. Hoạt động đúng với body gửi theo luồng không có `Content-Length` và với `Content-Length` khai sai (đếm byte thật); `413` không bị FastAPI đổi thành `400`; file tạm của Starlette (đã chuyển xuống đĩa khi > 1 MB) được đóng và xóa sau request, kể cả khi `413`. Hai điểm lệch envelope: `Content-Length` vượt giới hạn → middleware tự trả `413` dạng text thuần trước khi vào app; khi vượt giữa chừng → envelope JSON nhưng `code="http_error"` (sửa được bằng ánh xạ 413 trong `app/core/errors.py`).
- **RAM đo thật** (đỉnh private bytes, ảnh nhiễu là trường hợp xấu cho bộ mã hóa, 8–40 MP): JPEG ~8,2 byte/pixel và PNG ~8,2 byte/pixel khi phải xoay (2 ảnh 4 byte/pixel cùng tồn tại; không xoay: JPEG ~5,6, PNG ~7,1); **WebP ~34,4 byte/pixel** (giải mã ~16, mã hóa lại ~34), không phụ thuộc xoay. Giải phóng ảnh gốc ngay sau khi xoay giảm đỉnh 15–25%.

**Đã triển khai (2026-11-15):**
- `app/modules/media/`: `image_processing.py` (magic bytes → đọc header → giải mã + mã hóa lại trong `ImageProcessingGate`), `service.py` (luồng upload và hoàn tác), `router.py` (3 endpoint), `rate_limits.py`, `models.py` (`MediaAsset`), `schemas.py` (`MediaImageResponse` không có `storage_key`/`sha256`), `errors.py`.
- Thứ tự upload: rate limit IP (in-memory) → rate limit user (database) → ghi `tmp/` đếm byte thật → magic bytes + header → gate → ghi bản đã làm sạch vào `tmp/` → giữ chỗ hạn mức → `os.replace` sang `uploads/` → INSERT. Đổi chỗ lỗi: hoàn hạn mức, xóa file tạm. INSERT lỗi: xóa file, chỉ hoàn hạn mức khi xóa được (không xóa được thì giữ hạn mức, log `cần đối soát`). File tạm luôn bị xóa.
- `app/core/config.py`: `IMAGE_UPLOAD_MAX_BYTES` (10 MiB), `MAX_REQUEST_BODY_BYTES` (11 MiB, phải lớn hơn giới hạn ảnh). `app/core/errors.py`: `PayloadTooLargeError`, ánh xạ 413, `PayloadTooLargeEnvelopeMiddleware`. `app/core/dependencies.py`: `StorageDep`.
- `app/main.py`: `RequestBodyLimitMiddleware` + middleware chuẩn hóa 413, `app.state.storage`, `app.state.image_gate` (đóng trong lifespan), router media, `APP_CAPACITY_SHARES = AUTH_CAPACITY_SHARES + MEDIA_CAPACITY_SHARES` (media: IPv4 6, IPv6 /64 2, /48 2 trên tổng 110).
- **Thứ tự xử lý upload (sửa khi rà soát trước nghiệm thu, đã duyệt):** nếu endpoint khai báo tham số `File`, FastAPI parse toàn bộ multipart (tới 11 MiB, ghi file tạm khi > 1 MB) TRƯỚC mọi dependency, nên request chưa đăng nhập vẫn làm server đọc và ghi tạm cả body. Endpoint nay nhận `Request`: `CurrentUserDep` (session + CSRF) → rate limit IP → `MediaService.reserve_upload_rate_limit` → `request.form(max_files=1, max_fields=0)`. Request bị từ chối ở các bước đầu: app đọc 0 byte body. Hành vi thay đổi: 2 file hoặc field văn bản thừa → `400 http_error`. Response `422` khi thiếu file và schema OpenAPI multipart giữ nguyên (schema nay viết inline qua `openapi_extra`, không còn component tự sinh `Body_upload_image_...`).
- Migration `0005` (`down_revision="0004"`): bảng `media_assets`, index `(owner_id, created_at)` (phục vụ cả khóa ngoại), `storage_key` unique, CHECK `content_type`, `size_bytes`, `width`, `height` > 0.

### 6.4.1 Chi tiết P2-4.1 — An toàn commit file và quota trước cancellation (đã duyệt E1–E6 2026-11-15; đã triển khai và nghiệm thu 2026-11-15)

**Nguyên nhân (đã tái hiện bằng test RED trên code cũ):** hủy coroutine đang chờ `asyncio.to_thread` không dừng thread → `os.replace` vẫn có thể đặt file vào `uploads/` sau khi code cũ đã hoàn hạn mức; aiosqlite chạy COMMIT trong thread và PostgreSQL có thể mất kết nối giữa COMMIT → code cũ xóa file + hoàn hạn mức dù bản ghi đã commit.

**Đã sửa:**
- `app/core/storage.py`: `CommitOutcome` (`NOT_COMMITTED` / `COMMITTED` / `UNKNOWN`), `CommitAttempt`, `FileState`, `file_state(key)`. `commit_temp_file(temp, key, attempt=None)` (chữ ký cũ vẫn dùng được): hủy trước khi tạo thread → `NOT_COMMITTED`; hủy khi thread chạy → chờ thread xong (shield) rồi ném lại ĐÚNG `CancelledError` gốc; hủy lần nữa khi đang chờ → `UNKNOWN`; luôn đối chiếu với `lstat` file đích, lệch → `UNKNOWN`. Không dùng lớp con của `CancelledError` vì `asyncio.timeout()` so sánh đúng kiểu (`asyncio/timeouts.py` Python 3.12). Task đổi chỗ được giữ tham chiếu tới khi xong.
- `app/modules/media/service.py`: thao tác lưu chạy trên session RIÊNG (`session_factory`, E3); sha256 tính trước khi giữ chỗ; tách `_insert_asset` (add + flush) khỏi `_commit_asset` (E4). Mỗi giai đoạn có một kế hoạch dọn dẹp: giữ chỗ bị gián đoạn → không hoàn; đổi chỗ → chỉ hoàn khi `NOT_COMMITTED`; lỗi trước COMMIT → rollback, xóa, chỉ hoàn khi `file_state` xác nhận `ABSENT`; lỗi / hủy khi COMMIT → không xóa, không hoàn; lỗi thường thì kiểm tra lại bằng session mới theo đúng `owner_id` + `storage_key`, thấy bản ghi → trả 201 (E2).
- Dọn dẹp (E5 + điều kiện bổ sung): ĐÚNG MỘT task cho mỗi lần lỗi, được giữ tham chiếu (`pending_cleanups()`), sở hữu session lưu và tự đóng nó khi xong. Bị hủy lần hai trong lúc chờ: phía ngoài ném `CancelledError` ngay (không giả định `shield` làm phía ngoài chờ), task chạy tiếp tới hết — không gọi lại bước nào, không tạo task trùng, không hoàn hạn mức lần hai, không đóng session khi task còn dùng. Bước nào bị gián đoạn (kể cả chính task bị hủy) → không thử lại, không hoàn, log "cần đối soát".
- `app/modules/media/router.py`: `get_media_service` truyền `SessionFactoryDep`.

**Hành vi đã biết:** hủy lần hai khi `os.replace` còn chạy → khối `finally` xóa file tạm trước khi thread chạy tiếp, nên thường đổi chỗ thất bại (không có orphan); hạn mức vẫn giữ (tính THỪA) cho tới khi đối soát ở P2-8.

### 6.5 Chi tiết P2-5 — Danh mục model video (đã duyệt 2026-11-15; đã triển khai và nghiệm thu 2026-11-15)

**Quyết định của chủ dự án (D1–D10 và 4 điều kiện):**
- Phạm vi: chỉ danh mục model + `GET /video-models`. `providers/`, `VideoProvider`, `MockProvider` để sang P2-7. Không DB, không migration, không dependency mới.
- `app/modules/videos/catalog.py`: `AspectRatio` (StrEnum `16:9`, `9:16`, `1:1`), `ALLOWED_DURATIONS_SECONDS = (5, 10)`, `VideoModelSpec` (dataclass frozen, khả năng là tuple) — **nguồn duy nhất** về tỷ lệ, thời lượng, `supports_input_image`; router và config không lặp lại logic này. `KNOWN_VIDEO_MODELS` tách khỏi `VideoModelCatalog` (`enabled()`, `get_enabled()`, `get_known()`); `VideoModelSpec.supports()`.
- **Key model là định danh bất biến**, không phụ thuộc `display_name`; model đã tắt vẫn tra được bằng `get_known()`.
- Mock luôn `is_demo=true`, `display_name` ghi rõ không phải AI thật, ở mọi môi trường (kể cả staging/production).
- `ENABLED_VIDEO_MODELS` (JSON, mặc định `["mock"]`): `Settings` từ chối danh sách rỗng, key trùng, key sai định dạng (`[a-z0-9][a-z0-9-]{0,31}`); `create_app` từ chối key không có trong danh mục (`core` không phụ thuộc miền `videos`). Cả hai đều chạy khi khởi động.
- `GET /api/v1/video-models`: `CurrentUserDep`; `{"total", "items"}`; chỉ model đang bật, đúng thứ tự cấu hình; đúng 6 trường `key`, `display_name`, `is_demo`, `aspect_ratios`, `durations_seconds`, `supports_input_image`; `401` khi chưa đăng nhập; POST/PATCH/PUT/DELETE → `405`. Không rate limit riêng (dữ liệu tĩnh, không truy vấn DB).

**Dùng ở bước sau:** P2-6 kiểm tra `POST /videos` bằng `get_enabled(key)` + `spec.supports(...)`; P2-8 hiển thị video cũ bằng `get_known(key)`; worker (P2-7) tự dựng danh mục từ `Settings`. Server và worker phải dùng cùng `ENABLED_VIDEO_MODELS` (P2-7 quyết định cách xử lý job của model đã tắt).

### 6.6 Chi tiết P2-6 — API tạo video (đã duyệt 2026-11-15; đã triển khai và nghiệm thu 2026-11-15)

**Quyết định của chủ dự án (D1–D11 + bổ sung IntegrityError và hợp đồng quota 4.2):**
- D1: không giữ chỗ quota khi tạo job; chỉ kiểm tra sớm `usage >= quota` → `409 storage_quota_exceeded`. Worker giữ chỗ theo byte thật ở P2-7 (hợp đồng 4.2).
- D2: `queued` và `processing` đều chiếm slot. D3: `active_slot` (1, 2) + UNIQUE `uq_videos_owner_id_active_slot` + CHECK slot ⇔ trạng thái đang chạy. D4: có `GET /videos/{id}`; danh sách, nội dung, poster, xóa ở P2-8.
- D5: 60 lượt/IP/giờ (in-memory, `VIDEO_CAPACITY_SHARES` 6/2/2 gộp vào `APP_CAPACITY_SHARES`) và 30 lượt/user/giờ (`AccountThrottle`); lượt bị từ chối vẫn bị tính.
- D6: ảnh không tồn tại / của người khác / đã xóa → cùng `422 input_image_not_found`. D7: dùng lại `MediaService.get_image()`, không sửa media. D8: lease để P2-7 (migration 0007). D9: chưa có `Idempotency-Key` — request gửi lại có thể tạo job trùng (chặn một phần bởi giới hạn 2 job, rate limit; P2-10 chặn submit hai lần). D10: prompt bỏ khoảng trắng hai đầu, 1–1.000 code point, từ chối C0 trừ `\n\r\t`, CHECK độ dài ở DB, không ghi log. D11: phạm vi file như kế hoạch.

**Đã xây:**
- `app/modules/videos/models.py`: `Video`, `VideoStatus`; CHECK dựng từ hằng số của catalog (ORM luôn khớp code), migration `0006` ghi cố định cùng giá trị. CHECK cặp file (`output_*`, `poster_*`) dùng `IS NOT NULL AND size > 0` vì `NULL > 0` là NULL và CHECK coi NULL là đạt (test ràng buộc phát hiện khi triển khai).
- `app/modules/videos/service.py`: thứ tự kiểm tra model → tùy chọn → ảnh → hạn mức → chọn slot + INSERT. `classify_integrity_error`: SQLite theo `sqlite_errorname` + thông điệp khớp nguyên văn `UNIQUE constraint failed: videos.owner_id, videos.active_slot`; PostgreSQL theo `sqlstate` (`23505`, `23503`) + `constraint_name` của lỗi asyncpg. Chỉ `ACTIVE_SLOT_CONFLICT` được thử lại (tối đa 3 lần, rollback trước mỗi lần); lỗi FK → rollback rồi đọc lại ảnh (ảnh đã mất → 422, còn ảnh → ném lại = 500); lỗi khác → ném lại (500); hết 3 lần dù vẫn báo còn slot → 500. Không lỗi nào bị đổi thành 409.
- `app/modules/videos/router.py`: `POST /videos` (`202` + `Location`), `GET /videos/{id}`; `schemas.py`: `VideoCreate` (`StrictRequest`), `VideoResponse` (không có storage key, kích thước file, slot).
- Trạng thái: `queued → processing → succeeded | failed` (P2-7), chuyển trạng thái bằng CAS, câu chuyển sang trạng thái kết thúc trả `active_slot = NULL`.

**Kiểm thử đáng chú ý:** race tất định (`asyncio.Barrier`, hai request cùng đọc slot 1 → một request thử lại và nhận slot 2; kiểm thử đột biến tắt retry → test fail `[500, 202]`); 10 request đồng thời trên file SQLite thật → đúng 2 job (test này không tự gây va chạm trên SQLite, nên bằng chứng retry là test race tất định); ảnh bị xóa cứng giữa kiểm tra và INSERT → 422, không retry; user bị xóa giữa chừng → 500; vi phạm CHECK → 500; lỗi SQLite THẬT và lớp lỗi asyncpg thật của SQLAlchemy (bọc lỗi driver giả) cho bộ phân loại.

**Giới hạn đã chấp nhận:** PostgreSQL chưa được kiểm thử trên server thật (chưa cài asyncpg, chưa có server) — nhánh PostgreSQL của bộ phân loại chỉ được kiểm bằng lỗi driver giả; DDL `0006` đã kiểm offline bằng `alembic upgrade --sql`.

### 6.7 Kế hoạch P2-7 — Video Worker (kế hoạch đã duyệt 2026-11-15; đã triển khai, chờ nghiệm thu)

**Quyết định W1–W13 (đã duyệt):**

| # | Quyết định |
|---|---|
| W1 | Giữ trạng thái `succeeded` (không đổi sang `completed`) |
| W2 | Migration `0007`, `down_revision = "0006"`: `lease_token` String(32) NULL, `lease_expires_at` timestamptz NULL, `attempt_count` SmallInteger NOT NULL default 0 (CHECK ≥ 0), `next_attempt_at` timestamptz NULL; CHECK `ck_videos_lease_matches_status` (`processing` ⇔ có `lease_token` và `lease_expires_at`); index `(status, lease_expires_at)`; SQLite dùng `batch_alter_table`; không tự sửa dữ liệu |
| W3 | Claim bằng CAS. SQLite: một câu UPDATE có điều kiện (subquery + `AND status='queued'`). PostgreSQL: transaction `SELECT … FOR UPDATE SKIP LOCKED` rồi UPDATE lease trong cùng transaction. `attempt_count` chỉ tăng khi claim thành công. 1 job mỗi tiến trình |
| W4 | Lease 60 giây; heartbeat 10 giây là task riêng, độc lập với provider; poll 2 giây + jitter; tối đa 3 lần thử; backoff 15 giây × số lần thử |
| W5 | Chỉ lỗi "tạm thời" mới thử lại khi còn lượt (`OperationalError`, `TimeoutError`, `OSError` errno EAGAIN/EBUSY/EIO); lỗi lập trình và dữ liệu không hợp lệ → `failed` ngay |
| W6 | Model bị tắt hoặc ảnh đầu vào không còn khi nhận job → `failed` (`video_model_unavailable` / `input_image_unavailable`) |
| W7 | Mock: `MOCK_SAMPLE_VIDEO_PATH` (thiếu → job `failed` `mock_sample_missing`), `MOCK_PROCESSING_SECONDS=5`, `MOCK_FORCE_FAILURE=false` (từ chối `true` ở production), `VIDEO_OUTPUT_MAX_BYTES=100 MiB`, kiểm tra MP4 bằng `data[4:8] == b"ftyp"` |
| W8 | Registry task nền dùng chung trong mỗi tiến trình (`app/core/background_tasks.py`); media chuyển sang dùng, giữ `pending_cleanups()` |
| W9 | Lưu đầu ra trong `app/modules/videos/output.py` theo hợp đồng 4.2, không refactor luồng lưu của media |
| W10 | Dừng web: uvicorn `timeout_graceful_shutdown=20`; `lifespan` gọi `shutdown_background` (dưới đây) |
| W11 | Dừng worker: tín hiệu 1 → ngừng claim, chờ job ≤ 30 giây; quá hạn hoặc tín hiệu 2 → hủy job theo giai đoạn; rồi `shutdown_background`. `signal.signal` + `call_soon_threadsafe` (Windows không hỗ trợ `add_signal_handler`); SIGBREAK chỉ đăng ký khi `hasattr(signal, "SIGBREAK")` |
| W12 | Hủy có kiểm soát ở giai đoạn provider → requeue, không tính lượt thử (CAS có điều kiện, xem dưới) |
| W13 | Phạm vi file như danh sách dưới |

**`shutdown_background(registry, engine)` — dùng chung cho web và worker:**
1. Ngừng nhận việc mới (web: uvicorn ngừng nhận kết nối, hủy request quá `timeout_graceful_shutdown`; worker: cờ `stopping`).
2. Drain registry trong `SHUTDOWN_DRAIN_SECONDS` = 10 giây. Mỗi vòng lấy lại bản chụp `pending()` rồi `asyncio.wait(timeout=còn lại)`, nên task mới phát sinh trong lúc drain cũng được theo dõi. Registry chỉ bỏ một task qua `done_callback` sau khi task kết thúc; drain không bao giờ tự bỏ tham chiếu.
3. Hết hạn mà còn task → `cancel()` các task còn lại.
4. Drain lần hai trong `SHUTDOWN_CANCEL_GRACE_SECONDS` = 5 giây.
5. Vẫn còn task sống → log **critical** (số task, bước đang làm; không prompt, không đường dẫn tuyệt đối), registry **vẫn giữ tham chiếu**, **không `engine.dispose()`** khi task còn có khả năng dùng session hoặc connection.
6. Không còn task nào → mới `engine.dispose()` (web: sau `image_gate.close()`).
7. **Không bao giờ trả quota hay xóa file chỉ để kịp dừng.** Task bị hủy giữa chừng đi theo nhánh "dọn dẹp bị gián đoạn" của P2-4.1: không thử lại bước nào, không trả quota, log "cần đối soát".

**Không task nào được nuốt `CancelledError` vô hạn:** mọi task nền (dọn dẹp, requeue, heartbeat) khi bị hủy chỉ làm các bước có giới hạn trong `except`/`finally` (ghi log, `session.close()`), rồi **luôn ném lại** `CancelledError`. Không có vòng lặp bắt `CancelledError` để chạy tiếp.

**Khi event loop đóng (sau khi hàm chính trả về) và thời gian dừng:** các bước có thời hạn của chúng ta tối đa khoảng 45 giây (chờ job 30 + drain 10 + grace 5). **Đây không phải tổng thời gian tiến trình kết thúc.** Sau đó `asyncio.run` còn (a) hủy mọi task còn chờ và **chờ chúng kết thúc** (`gather(..., return_exceptions=True)`, task dọn dẹp ghi log rồi đóng session), (b) `shutdown_default_executor()` chờ các thread `to_thread` (`os.replace`, `unlink`, `lstat`) chạy xong — Python 3.12 cho phép tới 300 giây, (c) đóng loop. Thời gian thực tế vì vậy có thể dài hơn 45 hoặc 60 giây. Kết nối chưa được dispose sẽ đóng khi tiến trình thoát; database rollback phần chưa commit, nên quota chỉ có thể bị tính THỪA.

**Thời hạn supervisor:** 60 giây là thời hạn **dự kiến** đặt cho supervisor (Docker `stop_grace_period`, systemd `TimeoutStopSec`). Nếu tiến trình bị SIGKILL (hết thời hạn supervisor, hoặc `TerminateProcess` trên Windows) thì xử lý như **worker chết đột ngột**: lease hết hạn → reaper đưa job về `queued` (còn lượt, có tính lượt thử) hoặc `failed` `processing_timeout`; orphan file và quota tính thừa do P2-8 xử lý.

**Hủy theo giai đoạn** (ranh giới: giai đoạn output bắt đầu ngay trước `reserve`):
- *Provider (chưa lưu output):* xóa file tạm của provider. Dừng có kiểm soát → task requeue do registry theo dõi, session riêng: `UPDATE videos SET status='queued', lease_token=NULL, lease_expires_at=NULL, attempt_count=attempt_count-1, next_attempt_at=NULL WHERE id=:id AND status='processing' AND lease_token=:t AND output_storage_key IS NULL AND attempt_count > 0`. `rowcount 0` / lỗi / bị hủy → không làm gì thêm, reaper xử lý khi hết lease (có tính lượt thử). `lease_lost` → không requeue.
- *Output (từ `reserve` trở đi):* KHÔNG requeue. Xử lý theo hợp đồng 4.2 bằng `CommitAttempt`, `FileState`, quota và kết quả COMMIT; không xác định → giữ file + quota, log "cần đối soát". Job giữ `processing` cho tới khi reaper xử lý; reaper không đụng job đã `succeeded`.
- *Mất lease:* heartbeat CAS `rowcount 0` → `lease_lost`, hủy provider; kiểm tra `lease_lost` trước `reserve`. Mọi CAS kết thúc job (`succeeded` / `failed` / `queued`) đều có `AND status='processing' AND lease_token=:t`, nên worker mất lease không thể ghi `succeeded`; nếu mất lease khi đang lưu, CAS kết thúc `rowcount 0` → chắc chắn chưa commit → xóa file, xác nhận `ABSENT`, trả quota đúng 1 lần.

**File dự kiến:** mới — `app/worker.py`, `app/core/background_tasks.py`, `app/modules/videos/{jobs,heartbeat,output,runner}.py`, `app/modules/videos/providers/{__init__,base,mock,registry}.py`, migration `0007`. Sửa — `app/modules/videos/models.py`, `app/core/config.py` (mock, worker, shutdown; validator heartbeat < lease/2), `app/main.py` (`lifespan`), `app/server.py` (`timeout_graceful_shutdown`), `app/modules/media/service.py` (chỉ chuyển sang registry), `.env.example`, `README.md`, `CLAUDE.md`, kế hoạch. Không sửa auth, users, quota, logic upload ảnh; không thêm dependency.

**Test dự kiến (TDD):** registry (task mới trong lúc drain, không bỏ tham chiếu, tự gỡ khi xong); shutdown đường bình thường (dispose SAU drain) và còn task sống (log critical, `engine.dispose` không được gọi, tham chiếu còn); **task giả cố ý trì hoãn kết thúc** chờ một `asyncio.Event` giải phóng — test luôn đặt event trong `finally`, chờ task bằng `asyncio.wait_for(…, timeout)` và fixture kiểm tra registry rỗng sau test, nên test không thể treo; đóng event loop bằng subprocess (task nhận cancel, log "cần đối soát", `release` 0 lần, session đóng); web `lifespan` qua `app.router.lifespan_context(app)`; claim đồng thời nhiều engine (race tất định + kiểm thử đột biến), N job × M worker; SQL PostgreSQL compile có `FOR UPDATE SKIP LOCKED` (không có server); heartbeat độc lập (provider bị chặn, đồng hồ tiêm); mất lease; reaper; retry còn / hết lượt, lỗi lập trình `failed` ngay; hủy giai đoạn provider (CAS đủ điều kiện / sai điều kiện) và output (danh sách test hợp đồng 4.2, đếm `release`/`delete`); dừng worker (job xong trong hạn, quá hạn, tín hiệu 2, SIGBREAK có điều kiện); tiến trình thật `python -m app.worker` (`CTRL_BREAK_EVENT`, `terminate()` rồi lượt 2 hoàn tất); mock (`ftyp`, file < 8 byte, thiếu, thư mục, quá kích thước, `MOCK_FORCE_FAILURE`); migration `0007` trên DB tạm (head, `down_revision == "0006"`, CHECK, index, DDL PostgreSQL offline); chạy lặp 20 lần bộ test hủy / dừng / lease; regression P2-3 → P2-6.

**Rủi ro còn lại:** không dispose khi còn task sống (kết nối đóng khi tiến trình thoát, chỉ tính thừa); `shutdown_default_executor` có thể chờ tới 300 giây nếu thread I/O treo; SIGKILL từ supervisor → như worker chết (lease + P2-8); nhánh PostgreSQL chưa chạy trên server thật; SQLite nhiều tiến trình có thể `database is locked` (thử lại; khuyến nghị 1 worker); đồng hồ lệch nhiều máy (cần NTP); orphan và quota tính thừa cho tới P2-8; test phụ thuộc thời gian (đồng hồ tiêm, event có timeout, chạy lặp 20 lần).

**Ghi chú khi triển khai (2026-11-15):** `app/server.py` lấy `timeout_graceful_shutdown` từ `SERVER_GRACEFUL_SHUTDOWN_SECONDS` (mặc định 20). Cần đặt `PYTHONIOENCODING=utf-8` cho worker chạy trên console Windows để log tiếng Việt không bị đổi thành mã thoát. Hủy lần hai khi `os.replace` còn chạy ở giai đoạn lưu kết quả: khối `finally` xóa file tạm trước, nên thường không có orphan; hạn mức giữ (tính thừa) cho tới đối soát P2-8.

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
- **Upload của user ĐÃ đăng nhập vẫn tốn tài nguyên:** mỗi request hợp lệ được đọc tới 11 MiB (ghi file tạm của Starlette khi > 1 MB), giới hạn bởi 30 lượt/user/giờ và 60 lượt/IP/giờ nhưng chưa giới hạn số upload đồng thời mỗi user. Cổng xử lý ảnh và rate limit IP là theo từng tiến trình. Điều kiện reverse proxy ghi ở README (Điều kiện triển khai, mục 7–8).
- **Rule rate limit IP mới** cho upload/tạo video phải có tên riêng và sức chứa riêng. Từ P2-4, rule của từng miền nằm trong bảng tỉ trọng riêng (`MEDIA_CAPACITY_SHARES`…) và được gộp vào `APP_CAPACITY_SHARES` ở `app/main.py`; tỉ trọng là tương đối nên không cần chia lại tổng 100 của auth.
- **CSP và Swagger UI:** `/docs` cần script ngoài và inline; ngoại lệ chỉ áp dụng cho đúng `/docs`, `/redoc`, `/docs/oauth2-redirect` khi `APP_ENV=development`, không được nới CSP cho toàn app. Trang docs phụ thuộc CDN bên ngoài (jsdelivr, Google Fonts) nên cần internet khi xem.
