# AI Video Hub

Website **AI Video SaaS**: người dùng mua **credit trả trước** để tạo video **Text-to-Video** và **Image-to-Video** thông qua API của các nhà cung cấp AI bên ngoài. Hệ thống không tự chạy model trên GPU.

- **Kiến trúc**: Modular Monolith (FastAPI) + Worker riêng cho job AI (worker có từ giai đoạn 4).
- **Stack**: Python 3.12 · FastAPI · Pydantic v2 · SQLAlchemy 2 (async) · SQLite (dev) → PostgreSQL (mở rộng) · HTML/CSS/JS thuần.
- **Quy chuẩn lập trình**: [CLAUDE.md](CLAUDE.md) và skill [.claude/skills/ai-video-hub/SKILL.md](.claude/skills/ai-video-hub/SKILL.md).

## Trạng thái

| Giai đoạn | Nội dung | Trạng thái |
|---|---|---|
| 0 | Nền móng: khung app, config, DB, health, xử lý lỗi, công cụ kiểm tra | **Hoàn thành** (40 test, coverage 98.6%) |
| 1 | Auth và người dùng: đăng ký, đăng nhập session cookie + CSRF, phân quyền, profile, quên mật khẩu, rate limit, audit | **Hoàn thành**, đã khắc phục security review (250 test) |
| 2 | Thư viện video và lưu trữ | Chưa bắt đầu |
| 3 | Credit ledger | Chưa bắt đầu |
| 4 | Tạo video AI (MockProvider → nhà cung cấp thật) | Chưa bắt đầu |
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
│   └── time.py          # utc_now()
├── modules/             # mỗi miền nghiệp vụ là một module độc lập
│   ├── health/          # router · schemas · service
│   ├── users/           # User, UserRole, hồ sơ, danh sách user (ADMIN)
│   ├── auth/            # session, CSRF, Argon2id, reset mật khẩu, dependency phân quyền
│   └── audit/           # nhật ký thao tác nhạy cảm (bảng audit_events)
├── model_registry.py    # nạp mọi model cho Alembic
├── cli.py               # python -m app.cli promote-admin <email> [--yes] | cleanup-auth
└── server.py            # python -m app.server — cách khởi động chuẩn (tắt proxy headers của uvicorn)
migrations/              # Alembic (async) — 0001 auth tables, 0002 audit_events
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

Khi nâng phiên bản SQLAlchemy hoặc mypy thì đổi số phiên bản trong lệnh trên cho khớp. Coverage cũng không nạp được tracer C nhưng tự chuyển sang tracer Python, nên pytest sẽ hiện cảnh báo `no-ctracer`. Cảnh báo này vô hại.

## Chạy ứng dụng

```powershell
.\.venv\Scripts\python.exe -m app.server --reload                 # dev: http://127.0.0.1:8000
.\.venv\Scripts\python.exe -m app.server --host 0.0.0.0 --port 8000   # server thật (không --reload)
```

**Luôn khởi động bằng `python -m app.server`.** Launcher này tắt cơ chế tự tin header proxy của uvicorn: mặc định uvicorn tin `X-Forwarded-For` từ `127.0.0.1`, nên mọi tiến trình trên máy hoặc một proxy cấu hình sai đều có thể giả mạo IP client để né rate limit. Nếu buộc phải gọi uvicorn trực tiếp, thêm `--no-proxy-headers`.

- Health check: <http://127.0.0.1:8000/api/v1/health>
- Tài liệu API (OpenAPI): <http://127.0.0.1:8000/docs>, tự tắt khi `APP_ENV=production`.

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

Với `APP_ENV=staging` hoặc `production`, app **từ chối khởi động** nếu `DEBUG=true`, `COOKIE_SECURE=false`, hoặc `PUBLIC_BASE_URL` không phải `https://`. Riêng production còn cấm `EMAIL_BACKEND=file/memory`. Khi `COOKIE_SECURE=true`, cookie phiên mang tên `__Host-avh_session` (bắt buộc Secure, `Path=/`, không có Domain), nên subdomain không ghi đè được. Cookie CSRF vẫn tên `avh_csrf` ở mọi môi trường để JavaScript đọc được.

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
| POST | `/api/v1/auth/login` | — | `200` user + đặt cookie `avh_session` (HttpOnly) và `avh_csrf`; `401 invalid_credentials`; `429` |
| POST | `/api/v1/auth/logout` | CSRF nếu đang đăng nhập | `204`, thu hồi phiên phía server và xóa cookie |
| GET | `/api/v1/auth/me` | Đăng nhập | `200` user; `401` |
| POST | `/api/v1/auth/forgot-password` | — | `202` cùng một thông báo dù email có tồn tại hay không; `429` |
| POST | `/api/v1/auth/reset-password` | — | `200`; `400 invalid_reset_token` (sai, hết hạn hoặc đã dùng); `422`; `429` |
| PATCH | `/api/v1/users/me` | Đăng nhập + CSRF | `200`; chỉ đổi được `full_name`, gửi field khác như `role`/`email` sẽ bị `422` |
| GET | `/api/v1/users?skip=&limit=` | ADMIN | `200 {"total", "items"}`; `401`; `403` |

Mọi lỗi đều trả theo cùng một định dạng: `{"error": {"code": "...", "message": "...", "details": [...]}}`.

### Cách client dùng phiên đăng nhập

- Đăng nhập xong, trình duyệt tự gửi cookie `avh_session`. JavaScript **không đọc được** cookie này (HttpOnly) và cũng không cần lưu gì vào `localStorage`.
- Với mọi request `POST`/`PATCH`/`DELETE` khi đã đăng nhập, gửi header `X-CSRF-Token` có giá trị bằng cookie `avh_csrf`. Thiếu hoặc sai header thì nhận `403 csrf_failed`.
- Gọi `fetch` với `credentials: "same-origin"` (hoặc `"include"` nếu khác origin và origin đó có trong `ALLOWED_ORIGINS`).

### Thử luồng quên mật khẩu khi dev

Với `EMAIL_BACKEND=file`, mỗi email được ghi thành một file `.txt` trong `storage/dev-outbox/`. Thư mục này đã nằm trong `.gitignore`. Mở file mới nhất, lấy giá trị sau `#token=` rồi gọi `POST /api/v1/auth/reset-password`. Trên production, backend `file`/`memory` bị cấm và token không bao giờ xuất hiện trong response.
