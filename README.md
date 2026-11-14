# AI Video Hub

Website **AI Video SaaS**: người dùng mua **credit trả trước** để tạo video **Text-to-Video** và **Image-to-Video** thông qua API của các nhà cung cấp AI bên ngoài. Hệ thống không tự chạy model trên GPU.

- **Kiến trúc**: Modular Monolith (FastAPI) + Worker riêng cho job tạo video (tiến trình riêng `python -m app.worker`, có từ giai đoạn 2; không chạy trong tiến trình web server).
- **Stack**: Python 3.12 · FastAPI · Pydantic v2 · SQLAlchemy 2 (async) · SQLite (dev) → PostgreSQL (mở rộng) · HTML/CSS/JS thuần.
- **Quy chuẩn lập trình**: [CLAUDE.md](CLAUDE.md) và skill [.claude/skills/ai-video-hub/SKILL.md](.claude/skills/ai-video-hub/SKILL.md).

## Trạng thái

| Giai đoạn | Nội dung | Trạng thái |
|---|---|---|
| 0 | Nền móng: khung app, config, DB, health, xử lý lỗi, công cụ kiểm tra | **Hoàn thành** (40 test, coverage 98.6%) |
| 1 | Auth và người dùng: đăng ký, đăng nhập session cookie + CSRF, phân quyền, profile, quên mật khẩu, rate limit, audit | **Hoàn thành**: review đóng Phase 1 kết luận READY TO CLOSE (361 test, coverage 95,05%) |
| 2 | Video Creation UI: giao diện web, upload ảnh, tạo video qua hàng đợi + worker với **MockProvider** (demo, không có AI thật), thư viện video, lưu trữ có hạn mức theo user | **Đang triển khai**: P2-1 (khung web + header bảo mật) đã duyệt; P2-2 (giao diện đăng nhập/đăng ký/quên & đặt lại mật khẩu) đã duyệt; P2-3 (lưu trữ file + hạn mức 1 GiB/user, migration 0004) đã làm; P2-4 (API upload ảnh PNG/JPEG/WebP, loại metadata, migration 0005) đã nghiệm thu; P2-5 (danh mục model video + `GET /video-models`, chỉ có Mock demo) đã nghiệm thu; P2-6 (`POST /videos`, `GET /videos/{id}`, bảng `videos`, migration 0006) và P2-4.1 (lưu ảnh an toàn khi bị hủy / COMMIT không xác định) đã nghiệm thu; P2-7 (worker `python -m app.worker` + MockProvider demo, lease / retry, migration 0007) đã làm, chờ duyệt ([kế hoạch](docs/plans/phase-2-video-creation-ui.md)) |
| 3 | Credit ledger | Chưa bắt đầu |
| 4 | Tích hợp nhà cung cấp AI video thật (sau khi đã xác minh API và quyền sử dụng) | Chưa bắt đầu |
| 5 | Thanh toán (cổng thanh toán Việt Nam) | Chưa bắt đầu |
| 6 | Sẵn sàng production | Chưa bắt đầu |

## Cấu trúc

```text
app/
├── main.py              # create_app(): app factory, lifespan, CORS, router, exception handler
├── core/                # hạ tầng dùng chung cho mọi module
│   ├── config.py        # Settings (pydantic-settings, đọc .env)
│   ├── database.py      # engine async, session factory, Base, UTCDateTime, get_db
│   ├── client_ip.py     # IP thật của client (trusted proxy), khóa rate limit (IPv6 /64 + /48)
│   ├── dependencies.py  # SettingsDep, RateLimiterDep, EmailSenderDep, ClientIpDep, RateLimitSubjectDep
│   ├── email.py         # adapter EmailSender: file (dev) · memory (test) · disabled
│   ├── rate_limit.py    # RateLimiter in-memory, sức chứa riêng từng rule (Redis khi nhiều worker)
│   ├── errors.py        # AppError + các lớp lỗi + handler trả envelope {"error": {...}}
│   ├── storage.py       # LocalStorage: khóa do server sinh, chống path traversal/symlink, ghi tmp/ rồi os.replace
│   ├── background_tasks.py  # P2-7: registry task nền dùng chung + shutdown_background (không dispose khi còn task)
│   └── time.py          # utc_now()
├── modules/             # mỗi miền nghiệp vụ là một module độc lập
│   ├── health/          # router · schemas · service
│   ├── users/           # User, UserRole, hồ sơ, danh sách user (ADMIN)
│   ├── auth/            # session, CSRF, Argon2id, reset mật khẩu, dependency phân quyền
│   ├── audit/           # nhật ký thao tác nhạy cảm (bảng audit_events)
│   ├── quota/           # hạn mức dung lượng theo user (bảng user_storage_usage), giữ chỗ nguyên tử
│   ├── media/           # ảnh đầu vào (bảng media_assets): kiểm tra, mã hóa lại bằng Pillow, phục vụ theo quyền
│   └── videos/          # P2-5: danh mục model (catalog.py) + GET /video-models; P2-6: bảng videos, tạo job, đọc chi tiết
├── web/                 # các trang HTML tĩnh, mount /static, header bảo mật cho mọi response
│   ├── router.py        # PAGE_FILES: danh sách trắng đường dẫn → file trang + mount_static_files()
│   └── security_headers.py  # CSP, nosniff, X-Frame-Options, Referrer-Policy, Permissions-Policy, HSTS
├── model_registry.py    # nạp mọi model cho Alembic
├── cli.py               # python -m app.cli promote-admin <email> [--yes] | cleanup-auth
├── worker.py            # P2-7: python -m app.worker — worker tạo video (tiến trình riêng)
└── server.py            # python -m app.server — cách khởi động chuẩn (tắt proxy headers của uvicorn)
static/                  # HTML/CSS/JS thuần, không build step, không script/style inline
├── index.html · login.html · register.html · forgot-password.html · reset-password.html
├── css/                 # tokens.css (biến thiết kế) · base.css · forms.css
└── js/                  # api.js (điểm duy nhất gọi API) · session.js (header phiên) · authForm.js
                         #   · home.js · loginPage.js · registerPage.js · forgotPasswordPage.js · resetPasswordPage.js
migrations/              # Alembic (async) — 0001 auth tables, 0002 audit_events, 0003 account throttling + trusted devices, 0004 user_storage_usage, 0005 media_assets, 0006 videos, 0007 lease + retry của job video
tests/
├── conftest.py · helpers.py
├── unit/
└── integration/
```

Khi thêm một miền mới (auth, videos, credits, generations…), tạo `app/modules/<miền>/` gồm `router.py`, `schemas.py`, `service.py` và `models.py`, rồi đăng ký router trong `app/main.py`.

## Cài đặt trên Windows

Yêu cầu: **Python 3.12+**. Không cần Redis hay PostgreSQL ở giai đoạn này.

```powershell
cd "C:\AI Video Hub"
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m alembic upgrade head      # tạo / nâng cấp schema database
```

Muốn gõ ngắn `python`, `pytest`… thay cho đường dẫn đầy đủ thì kích hoạt venv bằng `.\.venv\Scripts\Activate.ps1`. Nếu PowerShell chặn script, chạy `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` một lần.

`requirements-lock.txt` ghi lại chính xác phiên bản đã kiểm thử. Muốn tái tạo đúng môi trường:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe -m pip install -e . --no-deps
```

### Windows có bật Smart App Control

Nếu gặp lỗi `ImportError: DLL load failed ... An Application Control policy has blocked this file`, nguyên nhân là Windows Smart App Control đang chặn các file `.pyd` biên dịch chưa ký. Lỗi này hay gặp ở SQLAlchemy và mypy. **Không cần tắt Smart App Control**, chỉ cần cài lại hai gói này bằng wheel thuần Python:

```powershell
.\.venv\Scripts\python.exe -m pip download sqlalchemy==2.1.8 mypy==2.4.1 --no-deps --only-binary=:all: --platform any --implementation py --python-version 3.12 --abi none -d .wheels
.\.venv\Scripts\python.exe -m pip install --force-reinstall --no-deps (Get-ChildItem .wheels\*.whl).FullName
```

**Pillow** (P2-4) không có wheel thuần Python; bản ghim `pillow==13.0.0` đã được kiểm tra import và chạy được khi Smart App Control đang bật. Nâng phiên bản Pillow phải kiểm tra lại import `PIL._imaging`, `PIL._webp`, `PIL._imagingcms` trong `.venv`.

Khi nâng phiên bản SQLAlchemy hoặc mypy thì đổi số phiên bản trong lệnh trên cho khớp. Coverage cũng không nạp được tracer C nhưng tự chuyển sang tracer Python, nên pytest sẽ hiện cảnh báo `no-ctracer`. Cảnh báo này vô hại.

## Chạy ứng dụng

```powershell
.\.venv\Scripts\python.exe -m app.server --reload                 # dev: http://127.0.0.1:8000
.\.venv\Scripts\python.exe -m app.server --host 0.0.0.0 --port 8000   # server thật (không --reload)
```

**Luôn khởi động bằng `python -m app.server`.** Launcher này tắt cơ chế tự tin header proxy của uvicorn: mặc định uvicorn tin `X-Forwarded-For` từ `127.0.0.1`, nên mọi tiến trình trên máy hoặc một proxy cấu hình sai đều có thể giả mạo IP client để né rate limit. Nếu buộc phải gọi uvicorn trực tiếp, thêm `--no-proxy-headers`.

- Giao diện web (bản demo): <http://127.0.0.1:8000/>, đăng nhập <http://127.0.0.1:8000/login>, đăng ký `/register`, quên mật khẩu `/forgot-password`, đặt lại mật khẩu `/reset-password` (mở từ link trong email).
- Health check: <http://127.0.0.1:8000/api/v1/health>
- Tài liệu API (OpenAPI): <http://127.0.0.1:8000/docs>, **chỉ bật khi `APP_ENV=development`**; `test`, `staging` và `production` trả `404` cho `/docs`, `/redoc`, `/docs/oauth2-redirect` và `/openapi.json`.

### Worker tạo video (P2-7)

```powershell
.\.venv\Scripts\python.exe -m app.worker                    # chạy liên tục, dừng bằng Ctrl+C / Ctrl+Break
.\.venv\Scripts\python.exe -m app.worker --exit-when-idle   # xử lý hết hàng đợi rồi thoát (thử nghiệm)
```

- **Tiến trình riêng**, khởi động / dừng độc lập với web server; hai bên chỉ trao đổi qua bảng `videos` và thư mục storage. Server vẫn chạy khi không có worker (job nằm ở `queued`).
- Phase 2 chỉ có **MockProvider — bản demo, không phải AI thật**: giả lập tiến độ rồi sao chép file MP4 mẫu ở `MOCK_SAMPLE_VIDEO_PATH` (do chủ dự án cung cấp, không commit). Thiếu file / không phải MP4 (`ftyp` ở byte 4..8) / lớn hơn `VIDEO_OUTPUT_MAX_BYTES` → job `failed` với mã lỗi rõ ràng, không thử lại.
- **Lease:** job đang xử lý giữ lease 60 giây, heartbeat gia hạn mỗi 10 giây. Worker chết đột ngột (SIGKILL, `TerminateProcess`, mất điện) → lease hết hạn → worker kế tiếp trả job về hàng đợi (có tính lượt thử) hoặc `failed` `processing_timeout` sau 3 lượt. File mồ côi và hạn mức tính thừa do bước đối soát P2-8 xử lý.
- **Thử lại:** chỉ lỗi tạm thời (DB bị khóa / mất kết nối, timeout, lỗi I/O tạm thời) khi còn lượt; lỗi dữ liệu và lỗi lập trình → `failed` ngay.
- **Dừng an toàn:** tín hiệu đầu tiên (SIGINT / SIGTERM / SIGBREAK) → ngừng nhận job mới, chờ job đang chạy tối đa `WORKER_SHUTDOWN_GRACE_SECONDS`; quá hạn hoặc tín hiệu thứ hai → hủy job: ở giai đoạn provider thì trả job về hàng đợi **không tính lượt thử**; ở giai đoạn lưu kết quả thì giữ file + hạn mức nếu không xác định được (log "cần đối soát"). Sau đó chờ task dọn dẹp nền; còn task không kết thúc → log critical và **không dispose engine**. Các thời hạn này chỉ giới hạn các bước của worker — tổng thời gian tiến trình kết thúc có thể dài hơn (asyncio còn hủy / chờ task và chờ thread).
- **Triển khai:** đặt thời hạn dừng của supervisor khoảng **60 giây** (Docker `stop_grace_period`, systemd `TimeoutStopSec`). Quá hạn supervisor gửi SIGKILL → xử lý như worker chết đột ngột (lease + P2-8). Với SQLite nên chạy **một** worker. Trên Windows đặt `PYTHONIOENCODING=utf-8` để log tiếng Việt hiển thị đúng khi ghi ra console / file.
- Web server khi tắt cũng chờ task dọn dẹp nền (upload ảnh) theo cùng quy tắc; uvicorn hủy request còn treo sau `SERVER_GRACEFUL_SHUTDOWN_SECONDS`.

### Giao diện web và header bảo mật

- Giao diện là HTML/CSS/JS thuần trong `static/`, được chính app phục vụ cùng origin với API: `/` trả `static/index.html`, `/static/...` trả file tĩnh. Không có build step, không cần Node.
- Mọi lệnh gọi API từ trình duyệt đi qua `static/js/api.js`. File này chỉ nhận đường dẫn `/api/v1/...` cùng origin và tự gửi header `X-CSRF-Token` cho request thay đổi dữ liệu.
- Các trang chỉ được phục vụ theo danh sách trắng `PAGE_FILES` trong `app/web/router.py`; đường dẫn ngoài danh sách trả `404`.
- Giao diện dùng lại nguyên API và cơ chế session/CSRF của Phase 1: header mọi trang gọi `GET /api/v1/auth/me` để hiện trạng thái phiên (`401` nghĩa là chưa đăng nhập, Console của trình duyệt sẽ ghi dòng 401 này — đã chấp nhận); đăng xuất gọi `POST /api/v1/auth/logout`. Đăng ký thành công sẽ tự đăng nhập; nếu bước đó lỗi, trang hướng dẫn đăng nhập thủ công.
- Trang `/reset-password` đọc token từ `#token=...`, xóa nó khỏi URL ngay bằng `history.replaceState`, chỉ giữ trong bộ nhớ JS (không `localStorage`/`sessionStorage`), và không gọi API nếu thiếu token hợp lệ. Khi dev, email nằm trong `EMAIL_OUTBOX_DIR` (mặc định `storage/dev-outbox/`, đã git-ignore); không gửi tới hộp thư thật.
- Mọi response (trang, file tĩnh, API, cả lỗi 4xx/5xx) có `Content-Security-Policy` chặt (không `unsafe-inline`/`unsafe-eval`), `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: strict-origin-when-cross-origin`, `Permissions-Policy`. `Strict-Transport-Security` chỉ gửi khi `COOKIE_SECURE=true` (chạy sau HTTPS).
- Vì CSP chặn script và style inline, mọi JS/CSS mới phải nằm trong file riêng dưới `static/`. Dữ liệu người dùng chỉ hiển thị bằng `textContent`.
- Ngoại lệ CSP duy nhất là `/docs`, `/redoc`, `/docs/oauth2-redirect` (Swagger UI / ReDoc cần script CDN và inline). Các trang này chỉ tồn tại khi `APP_ENV=development`; ở `test`, `staging` và `production` chúng trả `404` và không có ngoại lệ CSP nào được áp dụng.

App được dựng bằng factory `create_app()`, nên việc import module không đọc cấu hình. Thiếu `DATABASE_URL` thì app dừng ngay khi khởi động với thông báo lỗi rõ ràng.

### Reverse proxy và IP client

App tự xác định IP thật của client (dùng cho rate limit) theo `TRUSTED_PROXY_IPS`:

| Chế độ | Cấu hình | Hành vi |
|---|---|---|
| **Không có reverse proxy** (mặc định) | `TRUSTED_PROXY_IPS=[]` | Bỏ qua hoàn toàn `X-Forwarded-For`; IP là địa chỉ kết nối trực tiếp |
| **Có reverse proxy** (nginx, Caddy, load balancer) | `TRUSTED_PROXY_IPS=["10.0.0.5"]` (đúng IP/CIDR của proxy) | Chỉ đọc `X-Forwarded-For` khi kết nối đến từ proxy đã khai báo, lấy hop đầu tiên không phải proxy tính từ bên phải. Giá trị client tự chèn bị bỏ qua |

- Proxy phải **nối thêm** IP client vào cuối header (nginx: `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`), không được chuyển nguyên header của client.
- Wildcard (`*`, `0.0.0.0/0`, `::/0`) bị từ chối ở mọi môi trường.
- Rate limit với IPv6 tính theo prefix `/64`, vì một người dùng thường sở hữu cả dải này. Cả mạng `/48` chứa nó dùng chung ngân sách gấp 10 lần một client, để người giữ `/56` hay `/48` không nhân được giới hạn lên hàng trăm lần. Địa chỉ IPv4-mapped (`::ffff:a.b.c.d`) được tính như IPv4; IPv4 không bị gom theo mạng.
- **Giới hạn theo IP/mạng** nằm trong RAM: tổng `RATE_LIMIT_MAX_KEYS` (mặc định 100.000, khoảng 1,1 KB mỗi bộ đếm) chia riêng cho từng rule, nên làm đầy rule này không chiếm chỗ của rule khác. Khi một rule hết chỗ: IPv4 và `/48` từ chối client mới (429); `/64` bỏ qua dải mới (`/48` chứa nó vẫn chặn). Chỉ đúng khi chạy một tiến trình.
- **Giới hạn theo tài khoản** nằm trong database (bảng `auth_throttles`, dùng chung mọi worker): mỗi email được dồn 10 lượt đăng nhập rồi hồi 1 lượt mỗi 90 giây, kèm trần ngày dồn 100 lượt rồi hồi 1 lượt mỗi 14,4 phút (bị dò liên tục 24 giờ: đo được 199 lượt tới bước kiểm tra mật khẩu, sau đó khoảng 100 lượt/ngày); forgot-password dồn 3 lượt rồi hồi 1 lượt mỗi 5 phút, kèm trần ngày 10 lượt (đo được 19 email/24 giờ). Áp dụng như nhau cho email có tồn tại hay không, khóa là HMAC của email (`AUTH_THROTTLE_KEY`), không có sức chứa để làm đầy và không bao giờ bị bỏ qua. Đăng nhập thành công hoặc đặt lại mật khẩu sẽ xóa bộ đếm.
- **Thiết bị quen:** đăng nhập thành công đặt cookie `avh_device` (HttpOnly, 30 ngày). Trình duyệt đó có ngân sách đăng nhập riêng và không bị giới hạn IP, nên kẻ tấn công đốt hết ngân sách theo email cũng không khóa được chủ tài khoản. Đặt lại mật khẩu sẽ thu hồi mọi thiết bị quen.
- Chạy `python -m app.cli cleanup-auth` định kỳ để dọn session, token reset, thiết bị quen đã hết hạn và bộ đếm đã hồi.

### Staging và production

Với `APP_ENV=staging` hoặc `production`, app **từ chối khởi động** nếu `DEBUG=true`, `COOKIE_SECURE=false`, `PUBLIC_BASE_URL` không phải `https://`, `RATE_LIMIT_ENABLED=false`, `EMAIL_BACKEND` là `file`/`memory` (các backend này lưu token reset), hoặc `AUTH_THROTTLE_KEY` thiếu/ngắn hơn 32 ký tự. Khi `COOKIE_SECURE=true`, cookie phiên mang tên `__Host-avh_session` (bắt buộc Secure, `Path=/`, không có Domain), nên subdomain không ghi đè được. Cookie CSRF vẫn tên `avh_csrf` ở mọi môi trường để JavaScript đọc được.

### Điều kiện triển khai (bắt buộc trước staging/production)

1. **`AUTH_THROTTLE_KEY`**: chuỗi ngẫu nhiên ít nhất 32 ký tự (tạo bằng `python -c "import secrets; print(secrets.token_urlsafe(48))"`), **giống nhau ở mọi worker**. Nếu mỗi worker một khóa, bộ đếm theo tài khoản bị chia theo worker. App chỉ kiểm tra độ dài, không kiểm tra độ ngẫu nhiên.
2. **Giới hạn IP/mạng nằm trong RAM của từng tiến trình**: chỉ đúng khi chạy **một** process. Chạy nhiều worker hoặc nhiều máy thì cần kho đếm chung (Redis hoặc gateway, dự kiến giai đoạn 6). Giới hạn theo tài khoản đã nằm trong database nên dùng chung được.
3. **Đặt rate limit theo IP ở reverse proxy cho toàn bộ API.** Endpoint cần đăng nhập (vd. `PATCH /users/me`) chưa có rate limit riêng, và trình duyệt có cookie thiết bị quen thì bỏ qua giới hạn IP của `/login`.
4. **Chạy `python -m app.cli cleanup-auth` định kỳ** (Task Scheduler / cron, ví dụ mỗi giờ).
5. **PostgreSQL chưa được kiểm thử trên server thật**: migration mới chỉ được kiểm tra bằng DDL offline (`alembic upgrade --sql`). Trước production phải chạy migration và load test luồng đăng nhập trên PostgreSQL thật (lưu ý khả năng deadlock khi dọn bảng `auth_throttles` lúc tải cao; nếu xảy ra, request bị từ chối, không bị bỏ qua giới hạn).
6. Chạy server bằng `python -m app.server`, đặt `TRUSTED_PROXY_IPS` đúng dải của proxy.
7. **Reverse proxy cho upload ảnh (`POST /api/v1/media/images`)** — app đã từ chối request chưa đăng nhập, sai CSRF hoặc vượt rate limit **trước khi đọc body**, nhưng proxy vẫn phải chặn ở tầng mạng:
   - giới hạn body khoảng `11m` cho đường dẫn upload (vd. nginx `client_max_body_size 11m;`), các đường dẫn khác nhỏ hơn;
   - giới hạn số request và số kết nối đồng thời theo IP (vd. nginx `limit_req`, `limit_conn`);
   - timeout khi nhận body (vd. nginx `client_body_timeout`) để chặn client gửi rất chậm giữ kết nối;
   - lưu ý **proxy buffering**: mặc định nginx nhận hết body (ghi file tạm của proxy khi lớn) rồi mới chuyển cho app, nên áp lực RAM/đĩa tạm chuyển sang proxy và việc app từ chối sớm không còn tiết kiệm băng thông. Theo dõi dung lượng thư mục tạm của proxy, hoặc cân nhắc `proxy_request_buffering off` cho riêng đường dẫn upload.
8. **Giới hạn xử lý ảnh và rate limit IP là theo từng tiến trình**: cổng xử lý ảnh (2 ảnh cùng lúc, 4 request chờ) và bộ đếm IP nằm trong RAM của mỗi process. Chạy N worker thì RAM xử lý ảnh tối đa và ngân sách IP nhân lên N lần. Rate limit theo user (database) dùng chung mọi worker.

### Rủi ro đã chấp nhận khi đóng Phase 1

Mức LOW/INFO, đã được chủ dự án chấp nhận và chuyển sang các giai đoạn sau:

- Bộ đếm GCRA cho dồn hạn mức rồi hồi dần, nên cửa sổ đầu tiên cho tối đa gần gấp đôi (đo được 199 lượt đăng nhập trong 24 giờ đầu, sau đó khoảng 100 lượt/ngày).
- Cookie thiết bị quen nếu bị đánh cắp có thể phát lại từ nơi khác: không đăng nhập được nếu thiếu mật khẩu, nhưng được thêm ngân sách dò riêng (khoảng 199 lượt/ngày) và bỏ qua giới hạn IP cho đúng tài khoản đó. Chỉ bị thu hồi khi đặt lại mật khẩu.
- Kẻ tấn công liên tục vẫn chặn được đăng nhập từ **thiết bị mới** của nạn nhân; thiết bị quen hoặc đặt lại mật khẩu là lối thoát.
- `register` trả `409` khi email đã tồn tại (lộ email đã đăng ký) — cần luồng xác minh email.
- `promote-admin` chưa hiển thị trạng thái xác minh email của tài khoản.
- Khóa `AUTH_THROTTLE_KEY` rỗng ở dev thành khóa HMAC rỗng (production từ chối khóa ngắn).
- `Retry-After` có thể thấp hơn thực tế; người dùng chung một mạng IPv6 `/48` có thể ảnh hưởng ngân sách của nhau.

### Migration database (Alembic)

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head            # áp dụng mọi migration
.\.venv\Scripts\python.exe -m alembic current                 # phiên bản schema hiện tại
.\.venv\Scripts\python.exe -m alembic check                   # model và DB có lệch nhau không
.\.venv\Scripts\python.exe -m alembic revision --autogenerate -m "mo ta"   # tạo migration mới
```

URL database lấy từ `DATABASE_URL` (trong `.env`), không ghi trong `alembic.ini`. Giữ `alembic.ini` **chỉ chứa ký tự ASCII**: Alembic đọc file này theo bảng mã của Windows, nên comment tiếng Việt có dấu sẽ làm mọi lệnh `alembic` lỗi.

### Tạo tài khoản ADMIN

Đăng ký tài khoản bình thường qua API, rồi cấp quyền bằng CLI. Không có API nào cho phép tự nâng quyền.

```powershell
.\.venv\Scripts\python.exe -m app.cli promote-admin admin@example.com          # hỏi xác nhận
.\.venv\Scripts\python.exe -m app.cli promote-admin admin@example.com --yes    # script, không hỏi
```

CLI in ra database đích (đã che mật khẩu) trước khi làm gì. Lệnh từ chối khi tài khoản bị khóa, chỉ báo trạng thái nếu tài khoản đã là ADMIN, và ghi một dòng vào bảng `audit_events` (người thao tác, tài khoản được cấp quyền, thời điểm).

### Dọn session và token hết hạn

```powershell
.\.venv\Scripts\python.exe -m app.cli cleanup-auth
```

Lệnh xóa session và token đặt lại mật khẩu đã hết hạn hoặc bị thu hồi quá 7 ngày. Nên đặt chạy hằng ngày bằng Windows Task Scheduler (hoặc cron trên Linux). Chạy nhiều lần vẫn an toàn.

## Kiểm tra chất lượng

```powershell
.\.venv\Scripts\python.exe -m pytest                    # test + coverage (ngưỡng 80%)
.\.venv\Scripts\python.exe -m ruff check .               # lint
.\.venv\Scripts\python.exe -m ruff format --check .      # định dạng
.\.venv\Scripts\python.exe -m mypy                       # kiểm tra kiểu (strict)
.\.venv\Scripts\python.exe -m bandit -c pyproject.toml -r app   # quét bảo mật tĩnh
.\.venv\Scripts\python.exe -m pip_audit                  # lỗ hổng trong dependency
```

## API hiện có

| Method | Đường dẫn | Yêu cầu | Kết quả |
|---|---|---|---|
| GET | `/api/v1/health` | — | `200` trạng thái app và DB; `503 database_unavailable` |
| POST | `/api/v1/auth/register` | — | `201` user (role luôn là `user`); `409 email_already_registered`; `422`; `429` |
| POST | `/api/v1/auth/login` | — | `200` user + đặt cookie `avh_session` (HttpOnly), `avh_csrf` và lần đầu trên trình duyệt đó `avh_device` (HttpOnly, 30 ngày); `401 invalid_credentials`; `429` |
| POST | `/api/v1/auth/logout` | CSRF nếu đang đăng nhập | `204`, thu hồi phiên phía server và xóa cookie |
| GET | `/api/v1/auth/me` | Đăng nhập | `200` user; `401` |
| POST | `/api/v1/auth/forgot-password` | — | `202` cùng một thông báo dù email có tồn tại hay không; `429` |
| POST | `/api/v1/auth/reset-password` | — | `200`; `400 invalid_reset_token` (sai, hết hạn hoặc đã dùng); `422`; `429` |
| PATCH | `/api/v1/users/me` | Đăng nhập + CSRF | `200`; chỉ đổi được `full_name`, gửi field khác như `role`/`email` sẽ bị `422` |
| GET | `/api/v1/users?skip=&limit=` | ADMIN | `200 {"total", "items"}`; `401`; `403` |
| POST | `/api/v1/media/images` | Đăng nhập + CSRF, multipart field `file` | `201` metadata ảnh + `Location`; `409 storage_quota_exceeded`; `413 payload_too_large`; `415 unsupported_media_type` / `animated_image_not_supported`; `422 invalid_image` / `image_dimensions_too_large`; `429` (60/IP/giờ, 30/user/giờ); `503 image_processing_busy` + `Retry-After` |
| GET | `/api/v1/media/images/{id}` | Đăng nhập (chủ ảnh) | `200` metadata (`id`, `content_type`, `size_bytes`, `width`, `height`, `created_at`); `404` cả khi ảnh của người khác |
| GET | `/api/v1/media/images/{id}/content` | Đăng nhập (chủ ảnh) | `200` nội dung ảnh đã làm sạch, `Cache-Control: private, no-store`, `Cross-Origin-Resource-Policy: same-origin`; `404` |
| POST | `/api/v1/videos` | Đăng nhập + CSRF, JSON `{prompt, aspect_ratio, duration_seconds, model_key, input_image_id?}` | `202` video ở `queued` + `Location`; `409 active_video_limit_reached` (đã có 2 job `queued`/`processing`) / `storage_quota_exceeded`; `422 validation_error` / `video_model_unavailable` / `unsupported_video_options` / `input_image_not_supported` / `input_image_not_found`; `429` (60/IP/giờ, 30/user/giờ) |
| GET | `/api/v1/videos/{id}` | Đăng nhập (chủ video) | `200` trạng thái và thông tin video (không có storage key); `404` cả khi video của người khác |
| GET | `/api/v1/video-models` | Đăng nhập | `200 {"total", "items"}`: model đang bật theo thứ tự cấu hình, mỗi item có `key`, `display_name`, `is_demo`, `aspect_ratios`, `durations_seconds`, `supports_input_image`; `401` |

Mọi lỗi đều trả theo cùng một định dạng: `{"error": {"code": "...", "message": "...", "details": [...]}}`.

### Cách client dùng phiên đăng nhập

- Đăng nhập xong, trình duyệt tự gửi cookie `avh_session`. JavaScript **không đọc được** cookie này (HttpOnly) và cũng không cần lưu gì vào `localStorage`.
- Với mọi request `POST`/`PATCH`/`DELETE` khi đã đăng nhập, gửi header `X-CSRF-Token` có giá trị bằng cookie `avh_csrf`. Thiếu hoặc sai header thì nhận `403 csrf_failed`.
- Gọi `fetch` với `credentials: "same-origin"` (hoặc `"include"` nếu khác origin và origin đó có trong `ALLOWED_ORIGINS`).

### Thử luồng quên mật khẩu khi dev

**Lưu trữ file người dùng:** `STORAGE_ROOT` (mặc định `storage`, đã git-ignore) chứa `uploads/`, `videos/`, `posters/`, `tmp/`, được tạo khi ghi file đầu tiên. Đường dẫn tương đối tính từ thư mục dự án; app từ chối khởi động nếu `STORAGE_ROOT` nằm trong hoặc bao trùm `static/` hay `app/`. Hạn mức mỗi user là `STORAGE_QUOTA_BYTES_PER_USER` (mặc định 1 GiB = 1.073.741.824 byte), tính theo byte thực ghi xuống đĩa; vượt hạn mức trả `409 storage_quota_exceeded` (dùng từ P2-4). Sau khi cập nhật code, chạy `alembic upgrade head` để tạo bảng `user_storage_usage` và `media_assets`.

**Upload ảnh (P2-4):** chỉ nhận PNG, JPEG, WebP, xác định bằng magic bytes (bỏ qua `Content-Type` và tên file của client). Ảnh tối đa `IMAGE_UPLOAD_MAX_BYTES` (10 MiB), JPEG/PNG tối đa 24 MP, WebP tối đa 8 MP; ảnh động bị từ chối. Mọi ảnh được giải mã và mã hóa lại (JPEG/WebP quality 90, PNG lossless): áp hướng xoay EXIF, giữ ICC, loại EXIF/GPS/XMP/text. Hạn mức tính theo byte của file sau xử lý. Tối đa 2 ảnh được xử lý cùng lúc mỗi tiến trình, tối đa 4 request chờ, chờ quá 30 giây trả `503`. File chỉ đọc được qua `/content` sau khi kiểm tra chủ sở hữu, không qua `/static`. Chưa có API xóa ảnh.

**Model video (P2-5):** khả năng của từng model (tỷ lệ 16:9 / 9:16 / 1:1, thời lượng 5 / 10 giây, có nhận ảnh đầu vào) khai báo trong `app/modules/videos/catalog.py`; `ENABLED_VIDEO_MODELS` (mặc định `["mock"]`) chỉ chọn model nào được bật. Phase 2 chỉ có **Mock — bản demo, không phải nhà cung cấp AI thật**, ở mọi môi trường kể cả staging/production (`is_demo: true`). **Key model là định danh bất biến**: được lưu cùng mỗi video, không bao giờ đổi tên hay dùng lại cho model khác; muốn ngừng một model thì bỏ khỏi `ENABLED_VIDEO_MODELS` (model vẫn còn trong danh mục để hiển thị video cũ). Danh sách rỗng, key trùng, sai định dạng hoặc không có trong danh mục → app không khởi động.

**Tạo video (P2-6):** `POST /api/v1/videos` chỉ tạo job ở trạng thái `queued`; Phase 2 chưa có worker (P2-7) nên job chưa được xử lý và **không gọi nhà cung cấp AI nào**. Prompt bỏ khoảng trắng hai đầu, dài 1–1.000 ký tự (code point), không được chứa ký tự điều khiển (trừ xuống dòng và tab); prompt không bao giờ được ghi vào log. Model, tỷ lệ, thời lượng và việc nhận ảnh đầu vào kiểm tra theo `VideoModelSpec`. Ảnh đầu vào không tồn tại, của người khác hoặc đã xóa đều trả cùng `422 input_image_not_found`. Mỗi user tối đa **2 job đang chạy** (`queued` + `processing`), do database bảo đảm (`active_slot` + UNIQUE `uq_videos_owner_id_active_slot`), kể cả khi nhiều request đồng thời. Tạo job **không giữ chỗ dung lượng**: chỉ từ chối sớm khi đã dùng hết hạn mức; worker sẽ giữ chỗ theo kích thước file thật (kế hoạch 4.2). **Chưa có `Idempotency-Key`**: gửi lại cùng một request (mạng chập chờn, bấm hai lần) có thể tạo job trùng — được chặn một phần bởi giới hạn 2 job và rate limit; giao diện P2-10 phải chặn submit hai lần.

**An toàn hạn mức khi lưu ảnh (P2-4.1):** hạn mức CHỈ được hoàn lại khi chắc chắn file không còn trên đĩa và không có bản ghi đã lưu tham chiếu tới nó. Nếu request bị hủy (ví dụ khi tắt server) hoặc COMMIT trả lỗi mà không rõ đã lưu hay chưa, app giữ nguyên file và hạn mức, ghi log khóa tương đối kèm "cần đối soát" (đối soát ở P2-8). COMMIT báo lỗi nhưng bản ghi thực ra đã lưu → trả `201` như bình thường.

Thứ tự xử lý upload: đăng nhập + CSRF → rate limit IP → rate limit user → **rồi mới đọc body multipart**. Request bị từ chối ở các bước đầu không làm server đọc hay ghi tạm byte body nào. Body phải có đúng **một** file ở field `file` và không có field văn bản nào: gửi 2 file hoặc thêm field văn bản trả `400 http_error` (trước khi sửa thứ tự, các request này vẫn được nhận). Thiếu field `file` hoặc body không phải multipart vẫn trả `422 validation_error` (`body.file`) như cũ.

Với `EMAIL_BACKEND=file`, mỗi email được ghi thành một file `.txt` trong `storage/dev-outbox/`. Thư mục này đã nằm trong `.gitignore`. Mở file mới nhất, lấy giá trị sau `#token=` rồi gọi `POST /api/v1/auth/reset-password`. Trên production, backend `file`/`memory` bị cấm và token không bao giờ xuất hiện trong response.
