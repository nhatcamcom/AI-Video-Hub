# CLAUDE.md — AI Video Hub

Hướng dẫn bắt buộc cho Claude Code khi làm việc trong dự án này. Nội dung được rút ra từ các skill/rule **ECC** (repo `affaan-m/ECC`, commit `d2ef268`) đã đọc thực tế, điều chỉnh cho stack của dự án.

## 0. Bối cảnh

- **Backend:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.x.
- **Frontend:** HTML, CSS, JavaScript thuần (không React/Next.js).
- **Database:** SQLite (giai đoạn đầu) → PostgreSQL (giai đoạn mở rộng).
- **Kiến trúc:** Modular Monolith (FastAPI) + Worker riêng cho job tạo video (từ giai đoạn 2, chạy ở tiến trình riêng `python -m app.worker`).
- **Sản phẩm:** AI Video SaaS — Text-to-Video, Image-to-Video qua API nhà cung cấp bên ngoài (giai đoạn đầu dùng `MockProvider`; chỉ tích hợp nhà cung cấp thật khi đã xác minh API và quyền sử dụng). Credit trả trước, chi phí tính theo nhà cung cấp và tác vụ. Thanh toán (cổng Việt Nam) làm sau khi credit ổn định.
- **Trạng thái 2026-11-13:**
  - Giai đoạn 0 (nền móng) đã commit (`8769279`).
  - Giai đoạn 1 (Auth và người dùng) đã triển khai: session cookie phía server + CSRF, Argon2id, phân quyền USER/ADMIN, profile, quên mật khẩu, rate limit theo IP/mạng (in-memory) và theo tài khoản (database), cookie thiết bị quen, Alembic migration `0001`–`0003`, audit, cleanup. Review đóng Phase 1: **READY TO CLOSE** (361 test, coverage 95,05%). Điều kiện triển khai và rủi ro đã chấp nhận ghi trong `README.md`. PostgreSQL mới kiểm tra DDL offline, chưa chạy trên server thật.
  - Lộ trình và trạng thái từng giai đoạn ghi trong `README.md`.
- **Quy ước auth (bắt buộc giữ cho endpoint mới):**
  - Endpoint cần đăng nhập dùng `CurrentUserDep`; endpoint chỉ dành cho ADMIN dùng `AdminUserDep`. Hai dependency này đã tự kiểm tra CSRF cho POST/PATCH/PUT/DELETE, nên không tự viết lại logic kiểm tra session hay CSRF.
  - Mọi schema request kế thừa `StrictRequest` (`extra="forbid"`).
  - Quyền ADMIN chỉ được cấp qua `python -m app.cli promote-admin`.
  - Endpoint dễ bị lạm dụng phải gọi `enforce_rate_limits(limiter, checks)` một lần cho mọi rule của request, với `checks` dựng từ `RateLimitSubjectDep` (IPv4 theo địa chỉ; IPv6 theo `/64` kèm ngân sách chung cho cả `/48`) qua một `ClientRateLimit`, giống `login_checks` trong `app/modules/auth/rate_limits.py`. Không tự đọc `X-Forwarded-For`.
  - Rule IP mới phải có tên riêng và được thêm vào `AUTH_CAPACITY_SHARES` (sức chứa riêng). Rule theo IPv4 và `/48` là fail-closed; chỉ `/64` dùng `WhenFull.SKIP` (vì `/48` vẫn chặn).
  - Giới hạn theo tài khoản / email (chống dò mật khẩu phân tán, spam email) **không** dùng bộ nhớ in-memory mà dùng `AccountThrottle` (database, khóa HMAC, áp dụng như nhau cho email có tồn tại hay không). Không bao giờ để giới hạn bảo mật tự bỏ qua khi hết chỗ.
  - Băm/kiểm tra mật khẩu từ code async chỉ qua `hash_password_async` / `verify_password_async` (thread pool giới hạn RAM Argon2), không gọi `asyncio.to_thread` trực tiếp.
  - Mọi thao tác quản trị nhạy cảm (đổi quyền, sau này là credit) phải ghi `AuditService.record()` **trong cùng transaction** với thay đổi, và không ghi email hay secret vào audit hoặc log.
- **Chạy server:** luôn dùng `python -m app.server` (tắt `proxy_headers` của uvicorn). Việc tin header proxy chỉ do `TRUSTED_PROXY_IPS` quyết định. Không hướng dẫn chạy `uvicorn` trực tiếp mà thiếu `--no-proxy-headers`.
- **Chạy worker:** `python -m app.worker`, là **tiến trình riêng**, khởi động và dừng độc lập với web server. Không chạy worker bên trong tiến trình web server (không khởi động trong `lifespan` hay background task của `create_app()`). Server và worker chỉ trao đổi qua database (bảng job) và thư mục lưu trữ; server vẫn chạy được khi không có worker.
- **Engine database** luôn tạo qua `create_engine()` (app) hoặc `create_migration_engine()` (Alembic). Cả hai bật `hide_parameters=True`, nên lỗi SQL không làm lộ email, hash hay token vào log.

## Skill dự án `ai-video-hub` (đọc đầu tiên)

Skill tổng hợp của dự án nằm ở [.claude/skills/ai-video-hub/SKILL.md](.claude/skills/ai-video-hub/SKILL.md). Nó gom kiến thức thực tế từ các skill ECC, đã chuyển ví dụ sang Python/FastAPI/JS thuần, và gồm 13 mục: Architecture, Clean Code, Frontend, Backend, API Design, Database, TDD & Testing, Debugging, Security Review, Code Review, Git, Documentation, Verification.

- **Khi nào phải gọi `/ai-video-hub`** (hoặc công cụ Skill với tên `ai-video-hub`): **trước khi** đọc hay sửa code, mỗi khi làm việc với code của dự án, gồm:
  - phát triển tính năng
  - sửa lỗi
  - refactor
  - viết hoặc chạy test
  - review code
  - rà soát bảo mật
  - bảo trì (cập nhật dependency, migration DB, thay đổi API hoặc giao diện)
  - commit
  - cập nhật tài liệu kỹ thuật

  Sau khi gọi, đọc kỹ mục liên quan.
- **Khi nào không cần gọi**: câu hỏi thông thường không liên quan đến code của dự án, ví dụ giải thích khái niệm chung, hỏi đáp ngoài lề, hay câu hỏi về chính Claude Code. Trả lời trực tiếp.
- Thứ tự áp dụng: mục 1–6 khi thiết kế và viết code → mục 7 (TDD) trong khi viết → mục 13 (Verification) → mục 9 (Security Review) → mục 10 (Code Review) trước khi báo hoàn thành → mục 11 khi commit.
- Khi CLAUDE.md và SKILL.md mâu thuẫn, **CLAUDE.md được ưu tiên**. Hãy báo lại chỗ mâu thuẫn để cập nhật skill.
- Không sửa các skill ECC gốc trong `~/.claude/skills`. Mọi điều chỉnh riêng cho dự án ghi vào skill `ai-video-hub`.

## 1. Skill cần đọc trước khi làm việc

Trước mỗi nhiệm vụ, đọc skill liên quan (đọc nội dung, không chỉ đọc tên). Cột "Trạng thái" cho biết skill đã cài trong `~/.claude/skills` hay chưa.

| Loại việc | Skill / rule ECC | Trạng thái |
|---|---|---|
| Quy chuẩn chung, đặt tên, code smell | `coding-standards`, `rules/common/coding-style.md` | Đã cài / rule chưa cài |
| Viết code Python | `python-patterns`, `rules/python/*` | **Chưa cài** |
| Endpoint FastAPI, schema, DI, auth | `fastapi-patterns`, `rules/python/fastapi.md` | **Chưa cài** |
| Thiết kế REST API | `api-design` | **Chưa cài** |
| Kiến trúc backend (service, cache, log) | `backend-patterns` (ví dụ viết bằng TS, chỉ lấy nguyên tắc) | Đã cài |
| Xử lý lỗi | `error-handling` (có phần Python/FastAPI) | Đã cài |
| Schema, migration DB | `database-migrations`, `postgres-patterns` | **Chưa cài** |
| Viết test (TDD) | `tdd-workflow`, `python-testing` | Đã cài / **chưa cài** |
| Kiểm tra trước khi xong việc | `verification-loop` | Đã cài |
| Bảo mật | `security-review`, agent `security-reviewer` | **Chưa cài** |
| Code review | `rules/common/code-review.md`, agent `fastapi-reviewer`, `database-reviewer` | **Chưa cài** |
| Git | `git-workflow` | Đã cài |
| Giao diện HTML/CSS | `frontend-design-direction`, `accessibility`, `rules/web/*` | Đã cài / rule chưa cài |
| Kiểm thử E2E / QA trình duyệt | `e2e-testing`, `browser-qa` | Đã cài |
| Thêm thư viện hoặc tiện ích mới | `search-first` | Đã cài |
| Tài liệu | `docs-governance` (chỉ là bộ định tuyến và công cụ audit) | **Chưa cài** |

Skill **chưa cài** thì không đọc được ở phiên sau. Phải cài chúng vào `~/.claude/skills` (xem mục 10) trước khi viện dẫn. Không được tuyên bố đã áp dụng một skill mà chưa đọc nó trong phiên hiện tại hoặc chưa có trong tài liệu này.

`frontend-patterns` và `typescript-patterns` viết cho React/TypeScript, không áp dụng trực tiếp cho JS thuần, chỉ dùng phần nguyên tắc chung.

## 2. Nguyên tắc lập trình

1. **Đọc trước khi sửa.** Phân tích code hiện tại (file liên quan, hàm gọi đến, test) trước khi chỉnh. Không tự ý xóa hoặc viết lại chức năng đang chạy. Chỉ refactor khi có test bảo vệ và khi nhiệm vụ yêu cầu.
2. **Code dễ đọc là ưu tiên số 1.** Đặt tên rõ nghĩa, ưu tiên code tự giải thích. Comment để giải thích *tại sao*, không mô tả code *làm gì*.
3. **KISS / DRY / YAGNI.** Chọn giải pháp đơn giản nhất chạy được. Chỉ trừu tượng hóa khi đã thực sự lặp lại. Không xây trước tính năng chưa cần.
4. **Ưu tiên bất biến (immutability).** Tạo object mới thay vì sửa tại chỗ. Python: `@dataclass(frozen=True)`, `NamedTuple`, `model_copy(update=...)`. JS: spread `{...obj}`, `[...arr]`. Ngoại lệ hợp lệ là ORM entity của SQLAlchemy, vì nó được thiết kế để thay đổi trong session.
5. **Kích thước.** Hàm < 50 dòng. File 200–400 dòng là bình thường, trần mềm là 800 dòng. Lồng nhau không quá 4 cấp, dùng early return.
6. **Không dùng magic number.** Ngưỡng, thời gian chờ, giới hạn phải là hằng số có tên hoặc đặt trong `config`.
7. **Kiểm tra dữ liệu tại biên hệ thống.** Mọi input (request, file upload, API ngoài, file đọc vào) đều validate bằng schema Pydantic. Không tin dữ liệu bên ngoài.
8. **Không nuốt lỗi.** Mỗi `except` phải xử lý, raise lại hoặc log. Cấm `except:` trần và `except Exception: pass`.
9. **Type hint bắt buộc** trên mọi chữ ký hàm Python. Dùng kiểu hiện đại (`list[str]`, `X | None`).
10. **Tìm giải pháp có sẵn trước** khi tự viết tiện ích (skill `search-first`). Không thêm dependency khi chưa có lý do cụ thể.

## 3. Cấu trúc thư mục chuẩn

Modular Monolith, theo `fastapi-patterns` và `rules/python/fastapi.md`. Cấu trúc dưới đây là **cấu trúc thực tế từ giai đoạn 0**. Giữ nguyên cấu trúc hiện có; muốn đổi thì đề xuất trước, không tự ý di chuyển.

```text
AI Video Hub/
├── app/
│   ├── main.py            # create_app(): factory, lifespan, CORS, exception handler, router
│   ├── core/              # hạ tầng dùng chung — KHÔNG chứa nghiệp vụ
│   │   ├── config.py      # Settings (pydantic-settings, đọc .env), get_settings()
│   │   ├── database.py    # engine async, session factory, Base, get_db, DbDep
│   │   ├── dependencies.py# SettingsDep (và get_current_user… ở giai đoạn sau)
│   │   └── errors.py      # AppError + lớp con + handler + schema ErrorResponse
│   ├── worker.py          # `python -m app.worker` (từ giai đoạn 2): chỉ là entrypoint mỏng (vòng lặp, tín hiệu dừng, log);
│   │                      #   nghiệp vụ job nằm trong service của miền (app/modules/videos/)
│   └── modules/           # mỗi miền nghiệp vụ một thư mục (health, auth, videos, credits…)
│       └── <miền>/
│           ├── router.py  # router mỏng: nhận request, gọi service, trả response_model
│           ├── schemas.py # Pydantic: XxxCreate / XxxUpdate / XxxResponse tách riêng
│           ├── service.py # nghiệp vụ và transaction
│           └── models.py  # SQLAlchemy ORM models của miền
├── static/
│   ├── css/               # tokens.css (biến CSS), base.css, theo từng trang/khối
│   ├── js/                # Module ES theo tính năng; api.js gom các lệnh fetch
│   └── images/
├── templates/             # HTML (Jinja2 nếu render phía server)
├── migrations/            # Alembic async (env.py đọc DATABASE_URL từ Settings); alembic.ini CHỈ ASCII
│   └── versions/          # mỗi thay đổi schema = 1 file; model mới phải thêm vào app/model_registry.py
├── tests/
│   ├── conftest.py
│   ├── unit/
│   ├── integration/       # Test API + DB
│   └── e2e/               # Playwright (luồng quan trọng)
├── .env.example           # Biến môi trường mẫu, KHÔNG có giá trị thật
├── pyproject.toml
└── CLAUDE.md
```

- Tổ chức theo tính năng/miền (video, user, auth…), mỗi miền có router, schema, model, service riêng. Module chỉ gọi module khác qua service công khai của nó, không truy cập thẳng vào model của miền khác.
- App được dựng bằng factory `create_app()` và khởi động qua `python -m app.server`. Không tạo `app` ở mức module, để việc import không đọc cấu hình.
- Worker theo cùng mẫu với `app/server.py` và `app/cli.py`: `app/worker.py` là module chạy được bằng `python -m app.worker`, giữ mỏng; logic chiếm job, gọi provider, cập nhật trạng thái nằm trong service của miền `videos`. Nếu sau này entrypoint lớn quá một file thì đổi thành package `app/worker/` có `__main__.py`, lệnh chạy giữ nguyên. Không đặt worker ở thư mục gốc dự án.
- File video người dùng tải lên lưu **ngoài** `static/` và ngoài thư mục mã nguồn (ví dụ `storage/uploads/`, cấu hình qua `config`), và nằm trong `.gitignore`.

## 4. Quy tắc đặt tên

| Đối tượng | Quy tắc | Ví dụ |
|---|---|---|
| Biến, hàm Python | `snake_case`, hàm theo dạng động từ + danh từ | `fetch_video_by_id`, `total_duration_seconds` |
| Class Python | `PascalCase` | `VideoService`, `VideoCreate` |
| Hằng số | `UPPER_SNAKE_CASE` | `MAX_UPLOAD_SIZE_BYTES` |
| Biến boolean | Tiền tố `is_` / `has_` / `can_` | `is_published`, `has_thumbnail` |
| Module Python | `snake_case.py` | `video_service.py` |
| Schema Pydantic | `<Thực thể><Mục đích>` | `VideoCreate`, `VideoUpdate`, `VideoResponse`, `VideoListResponse` |
| Ngoại lệ | Đuôi `Error` | `VideoNotFoundError`, `DuplicateUserError` |
| Biến, hàm JS | `camelCase` | `loadVideoList`, `isPlaying` |
| Class JS | `PascalCase` | `VideoPlayer` |
| File JS | `camelCase.js` | `videoPlayer.js` |
| Class CSS / file CSS | `kebab-case` | `.video-card`, `video-card.css` |
| Bảng / cột DB | `lowercase_snake_case`, tên bảng số nhiều | `videos`, `created_at` |
| URL API | Danh từ số nhiều, `kebab-case`, có version | `/api/v1/videos`, `/api/v1/videos/{video_id}/view-stats` |
| Trường JSON | `snake_case` (khớp Pydantic), nhất quán toàn API | `thumbnail_url` |
| Test | `test_<hành_vi>_<điều_kiện>` | `test_create_video_rejects_oversized_file` |

Tránh tên mơ hồ như `data`, `tmp`, `x`, `flag`, `handle()`.

## 5. Quy trình viết và kiểm tra code

Áp dụng cho **mọi** module hoặc tính năng:

1. **Đọc skill** liên quan (mục 1).
2. **Phân tích**: đọc code và test hiện có, xác định phạm vi ảnh hưởng và những gì không được làm vỡ.
3. **Lập kế hoạch ngắn**: viết user journey ("Là [vai trò], tôi muốn [hành động], để [lợi ích]"), liệt kê ca test (happy path, edge case, lỗi, biên).
4. **RED**: viết test trước, chạy và xác nhận nó **fail đúng lý do** (fail vì thiếu chức năng, không phải vì lỗi cú pháp hay thiếu thư viện). Test chưa chạy thì chưa tính là RED.
5. **GREEN**: viết code tối thiểu để test pass, chạy lại đúng test đó.
6. **REFACTOR**: làm sạch code khi test vẫn xanh.
7. **Verification loop** sau mỗi module:
   ```bash
   .venv/Scripts/python.exe -m pytest                        # test + coverage (ngưỡng 80% cấu hình trong pyproject)
   .venv/Scripts/python.exe -m ruff check .                  # lint
   .venv/Scripts/python.exe -m ruff format --check .         # định dạng (ruff thay black/isort)
   .venv/Scripts/python.exe -m mypy                          # kiểm tra kiểu strict (app + tests)
   .venv/Scripts/python.exe -m bandit -c pyproject.toml -r app  # quét bảo mật tĩnh
   .venv/Scripts/python.exe -m pip_audit --skip-editable     # lỗ hổng trong dependency
   ```
   Luôn dùng Python trong `.venv`, không cài package toàn cục.
   Nếu một công cụ chưa được cài, báo rõ là **chưa chạy được**, không ghi PASS.
8. **Security review** (mục 7) và **review** (mục 8) trước khi báo hoàn thành.

Quy ước test:
- pytest + pytest-asyncio. Test API dùng `httpx.AsyncClient(transport=ASGITransport(app=...))`. App có lifespan thì bọc bằng `asgi_lifespan.LifespanManager`.
- DB test dùng SQLite in-memory và override **đúng** dependency `get_db`, sau đó xóa `app.dependency_overrides` khi xong.
- Cấu trúc AAA (Arrange, Act, Assert). Mỗi test độc lập, không phụ thuộc thứ tự, mock dịch vụ bên ngoài (AI API, lưu trữ).
- Không sửa test để nó pass, trừ khi chính test sai.

## 6. Quy trình phát hiện và sửa lỗi

ECC không có skill debugging riêng. Quy trình dưới đây ghép từ `tdd-workflow` (phần sửa bug), `error-handling`, `rules/common/testing.md` và `verification-loop`.

1. **Tái hiện**: xác định chính xác triệu chứng, input, log, traceback. Không đoán.
2. **Viết test tái hiện (reproducer)** và chạy để thấy nó fail đúng như lỗi, đây là RED.
3. **Tìm nguyên nhân gốc**: đọc luồng code liên quan, kiểm tra cách ly test, kiểm tra mock. Sửa nguyên nhân, không vá triệu chứng.
4. **Sửa tối thiểu** rồi chạy lại reproducer cho đến khi xanh (GREEN), sau đó chạy **toàn bộ** test suite để chắc không có hồi quy.
5. **Tìm lỗi tương tự** ở chỗ khác trong codebase, nhất là với lỗi bảo mật.
6. **Báo cáo trung thực**: nguyên nhân, cách sửa, lệnh đã chạy, kết quả thực tế.

Chuẩn xử lý lỗi:
- Có lớp gốc `AppError(message, code, status_code)` và các lớp con (`NotFoundError` 404, `ValidationError` 422, `UnauthorizedError` 401, `ForbiddenError` 403, `ConflictError` 409, `RateLimitError` 429).
- Đăng ký exception handler toàn cục trong `create_app()`: `AppError` trả về đúng mã lỗi của nó, `Exception` trả 500 với thông điệp chung, còn chi tiết chỉ ghi vào log server (`logger.exception`).
- Định dạng lỗi thống nhất: `{"error": {"code": "...", "message": "...", "details": [...]}}`. Mặc định FastAPI trả `{"detail": ...}`, nên cần handler cho cả `HTTPException` và `RequestValidationError` để đưa về cùng định dạng.
- Thông điệp cho người dùng tách khỏi thông điệp cho dev. Không bao giờ trả stack trace hoặc lỗi SQL ra client.
- Dùng `raise ... from exc` để giữ chuỗi nguyên nhân. Chỉ retry (exponential backoff + jitter) với lỗi tạm thời (timeout, 5xx), không retry lỗi 4xx.
- Phía JS: mọi lệnh `fetch` kiểm tra `response.ok`, hiển thị thông báo thân thiện và không để lỗi biến mất trong im lặng.

## 7. Bảo mật API và cơ sở dữ liệu

**Bí mật (secrets)**
- Không hardcode API key, mật khẩu, token. Đọc qua `pydantic-settings` từ biến môi trường. Thiếu biến bắt buộc thì app dừng ngay khi khởi động.
- `.env` nằm trong `.gitignore`. Chỉ commit `.env.example` với giá trị giả. Nếu một secret từng bị lộ thì phải xoay (rotate) nó.

**Xác thực và phân quyền**
- Mật khẩu hash bằng bcrypt hoặc argon2, không so sánh plaintext.
- JWT phải kiểm tra chữ ký, `exp`, thuật toán (cố định danh sách `algorithms`), và cả issuer/audience nếu có dùng. Trên web nên lưu token trong cookie `HttpOnly; Secure; SameSite`, **không dùng `localStorage`**.
- Mọi route không công khai phải có dependency xác thực. Kiểm tra quyền sở hữu tài nguyên (người dùng chỉ sửa/xóa video của mình). Phân biệt rõ 401 (chưa xác thực) và 403 (không có quyền).
- Response model **không bao giờ** chứa password, hash, token hay trạng thái auth nội bộ. Luôn khai báo `response_model`.

**API**
- Validate mọi input bằng Pydantic (ràng buộc `Field`, `Query(ge=, le=)`), giới hạn `limit` khi phân trang (ví dụ tối đa 100).
- CORS: origin cấu hình theo môi trường. Không kết hợp `allow_origins=["*"]` với `allow_credentials=True`.
- Rate limit cho endpoint đăng nhập, upload và các thao tác ghi tốn tài nguyên (gọi AI). Bộ đếm phải dùng kho chung (Redis/gateway) khi chạy nhiều instance.
- Form thay đổi trạng thái cần chống CSRF (SameSite cookie + token).
- Header bảo mật cho production: CSP chặt (không dùng `unsafe-inline`/`unsafe-eval` mặc định), `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`, HSTS khi đã có HTTPS.
- Không gọi `fetch`/`httpx` tới URL do người dùng cung cấp nếu chưa kiểm tra theo whitelist (chống SSRF). Gọi HTTP ra ngoài luôn phải đặt timeout.

**Upload video và file**
- Kiểm tra kích thước, MIME type **và** phần mở rộng theo whitelist. Đặt tên file ngẫu nhiên phía server, không dùng tên người dùng gửi.
- Chống path traversal: không ghép trực tiếp đường dẫn từ input. Resolve đường dẫn rồi kiểm tra nó vẫn nằm trong thư mục lưu trữ.
- Không truyền input người dùng vào lệnh shell (ví dụ khi gọi ffmpeg thì dùng danh sách tham số, không dùng `shell=True`).

**Frontend (XSS)**
- Hiển thị dữ liệu người dùng bằng `textContent`, không dùng `innerHTML`. Nếu buộc phải render HTML thì sanitize trước. Template Jinja2 giữ autoescape.
- Script từ CDN phải có SRI, ưu tiên tự host thư viện quan trọng.

**Cơ sở dữ liệu**
- Chỉ dùng ORM hoặc truy vấn có tham số (`select(...).where(...)`, `text(...)` với bind params). **Cấm** f-string/nối chuỗi trong SQL.
- Tính duy nhất được đảm bảo bằng ràng buộc DB (`unique=True`), bắt `IntegrityError` rồi rollback. Không chỉ dựa vào kiểm tra phía ứng dụng.
- Transaction đặt ở tầng service và giữ ngắn: không giữ transaction trong lúc gọi API AI hoặc xử lý video.
- Không dùng `SELECT *` trong code production, chỉ lấy cột cần thiết. Tránh N+1 bằng `selectinload`/`joinedload` hoặc batch.
- Phân trang có `ORDER BY` xác định (`.order_by(Model.id)`). Bảng lớn hoặc feed dùng cursor pagination.
- Đánh index cho cột trong WHERE/JOIN và **mọi khóa ngoại**. Composite index: cột so sánh bằng đứng trước, cột so sánh khoảng đứng sau.
- Không log password, token, header `Authorization`, cookie.

**Chuẩn bị chuyển SQLite → PostgreSQL**
- `DATABASE_URL` lấy từ config, code không phụ thuộc loại DB cụ thể. Không dùng SQL riêng của SQLite.
- Kiểu dữ liệu tương thích PostgreSQL: thời gian dùng `DateTime(timezone=True)` (tương ứng `timestamptz`), chuỗi dùng `Text`/`String`, tiền dùng `Numeric`, cờ dùng `Boolean`.
- Mọi thay đổi schema đi qua migration (Alembic). Không sửa DB production bằng tay, không sửa migration đã chạy. Migration schema tách riêng migration dữ liệu. Cột mới phải nullable hoặc có default. Trên PostgreSQL, index cho bảng lớn tạo bằng `CREATE INDEX CONCURRENTLY`. Đổi tên/xóa cột theo mô hình expand → migrate → contract.
- Sử dụng PostgreSQL: user ứng dụng theo nguyên tắc quyền tối thiểu (không `GRANT ALL`), đặt `statement_timeout`, dùng connection pooling.

## 8. Review trước khi hoàn thành một chức năng

Không báo "xong" nếu chưa đi hết danh sách này và chưa có bằng chứng (output lệnh thật).

**Đúng chức năng**
- [ ] Giải quyết đúng yêu cầu, không phá chức năng đang chạy, không có thay đổi ngoài phạm vi.
- [ ] Đã xử lý edge case và lỗi.

**Chất lượng code**
- [ ] Tên rõ ràng, hàm < 50 dòng, file < 800 dòng, lồng không quá 4 cấp.
- [ ] Router mỏng, logic nằm trong service, schema request/response tách riêng.
- [ ] Không còn `print()`/`console.log` debug, không còn code bị comment bỏ.
- [ ] Không có giá trị hardcode (dùng hằng số hoặc config).

**Test và kiểm tra**
- [ ] Đã có test cho chức năng mới, có bằng chứng RED và GREEN.
- [ ] Toàn bộ test pass, coverage ≥ 80%.
- [ ] Lint, type check, bandit, pip-audit đã chạy (hoặc ghi rõ là chưa chạy được và lý do).

**Bảo mật** (bắt buộc khi đụng tới auth, input người dùng, DB, file, API ngoài)
- [ ] Không có secret trong code, input đã được validate, SQL có tham số, có kiểm tra auth và phân quyền.
- [ ] Response không lộ dữ liệu nhạy cảm, thông báo lỗi chung chung, log không chứa thông tin nhạy cảm.

**API**
- [ ] URL, method, status code đúng quy ước (201 + `Location` khi tạo mới, 204 khi xóa, 404/409/422 đúng ngữ cảnh).
- [ ] Endpoint danh sách có phân trang, tài liệu OpenAPI đầy đủ (`response_model`, mô tả lỗi).

**Frontend** (nếu có thay đổi giao diện)
- [ ] HTML có ngữ nghĩa, có nhãn cho input và nút icon, focus nhìn thấy được, độ tương phản ≥ 4.5:1, vùng chạm ≥ 24×24px.
- [ ] Có đủ trạng thái loading / empty / error / success. Đã kiểm tra hiển thị ở màn hình hẹp và rộng.

**Mức độ nghiêm trọng khi review:** CRITICAL (lỗ hổng bảo mật, mất dữ liệu) thì **chặn**, phải sửa ngay. HIGH (bug, vấn đề chất lượng lớn) thì phải sửa trước khi merge. MEDIUM thì nên sửa. LOW là tùy chọn.

**Báo cáo khi xong:** liệt kê file đã thay đổi, các lệnh đã chạy kèm kết quả thực tế, những gì chưa kiểm tra được và rủi ro còn lại.

## 9. Git

- Repo Git trên nhánh `main`. Commit đầu tiên `8769279 chore: bootstrap phase 0 foundation`.
- Commit theo Conventional Commits: `feat(video): ...`, `fix(api): ...`, `test: ...`, `refactor: ...`, `docs: ...`, `chore: ...`.
- Nhánh: `feature/<tên>`, `fix/<tên>`, `main` luôn chạy được. Chỉ commit hoặc push khi người dùng yêu cầu.
- Không commit `.env`, file video, thư mục upload, `__pycache__/`, `.venv/`, file `*.db` của SQLite, báo cáo coverage.
- Không force-push lên nhánh dùng chung, dùng `git revert` để hoàn tác thay đổi đã public.

## 10. Việc còn tồn đọng về môi trường

- Các skill ECC **chưa cài** cần cho dự án: `python-patterns`, `python-testing`, `fastapi-patterns`, `api-design`, `database-migrations`, `postgres-patterns`, `security-review` (kèm file `cloud-infrastructure-security.md`), `docs-governance`. Rule `rules/common`, `rules/python`, `rules/web` và agent `fastapi-reviewer`, `security-reviewer`, `database-reviewer`, `python-reviewer`, `code-reviewer` cũng chưa cài. Chỉ cài khi người dùng đồng ý.
- Công cụ Python đã được cài trong `.venv` (không cài toàn cục): fastapi, sqlalchemy, aiosqlite, pydantic-settings, uvicorn, pytest, pytest-asyncio, pytest-cov, httpx, ruff, mypy, bandit, pip-audit.
- **Smart App Control của Windows đang BẬT** trên máy dev. Nó chặn các file `.pyd` biên dịch chưa ký. Hướng xử lý: SQLAlchemy và mypy được cài từ wheel **thuần Python** (`py3-none-any`), các bước nằm trong README. Coverage tự chuyển sang tracer Python nên luôn có cảnh báo `no-ctracer`. Tracer này không theo dõi được code chạy sau `await` của SQLAlchemy (greenlet), nên số liệu mặc định **thấp hơn thực tế** (khoảng 94% so với 100% thật ở Phase 1). Muốn đo **line coverage chính xác**, chạy với một file cấu hình coverage riêng có `branch = false` và `core = sysmon`: `pytest -o addopts="" --cov=app --cov-config=<file đó> --cov-report=term-missing`. **Không** được tắt Smart App Control thay người dùng. Khi thêm dependency có phần biên dịch, phải kiểm tra import được trong `.venv`.
