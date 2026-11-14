"""Cấu hình ứng dụng: đọc từ biến môi trường / file .env, kiểm tra ngay khi khởi động."""

import secrets
from functools import cache, cached_property, lru_cache
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.client_ip import IPNetwork, parse_trusted_proxies

AppEnvironment = Literal["development", "test", "staging", "production"]
EmailBackend = Literal["file", "memory", "disabled"]

WILDCARD_ORIGIN = "*"
DEVELOPMENT_EMAIL_BACKENDS: frozenset[str] = frozenset({"file", "memory"})
HARDENED_ENVIRONMENTS: frozenset[str] = frozenset({"staging", "production"})
HOST_COOKIE_PREFIX = "__Host-"
MIN_THROTTLE_KEY_LENGTH = 32

# STORAGE_ROOT tương đối tính từ thư mục dự án (không phụ thuộc thư mục đang chạy lệnh)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
# File người dùng không bao giờ được nằm trong (hoặc bao trùm) mã nguồn và thư mục phục vụ công khai
STORAGE_FORBIDDEN_DIRS: tuple[Path, ...] = (PROJECT_ROOT / "static", PROJECT_ROOT / "app")
DEFAULT_STORAGE_QUOTA_BYTES = 1_073_741_824  # 1 GiB/user (quyết định 7.5)
MEBIBYTE = 1024 * 1024
DEFAULT_IMAGE_UPLOAD_MAX_BYTES = 10 * MEBIBYTE  # 10.485.760 byte (P2-4)
# Ảnh 10 MiB + phần đầu/cuối của multipart; áp cho MỌI request (body JSON hiện tại rất nhỏ)
DEFAULT_MAX_REQUEST_BODY_BYTES = 11 * MEBIBYTE


@cache
def _development_throttle_key() -> bytes:
    """Khóa ngẫu nhiên cho dev/test khi chưa cấu hình: chỉ đúng với MỘT tiến trình."""
    return secrets.token_bytes(MIN_THROTTLE_KEY_LENGTH)


def resolve_storage_root(raw_root: str) -> Path:
    """Đường dẫn tuyệt đối (đã resolve symlink, chuẩn hóa '..') của STORAGE_ROOT. Không tạo thư mục.

    Raises:
        ValueError: nếu thư mục nằm trong, trùng, hoặc là thư mục cha của static/ hay app/.
    """
    candidate = Path(raw_root)
    root = (candidate if candidate.is_absolute() else PROJECT_ROOT / candidate).resolve()
    for forbidden in (directory.resolve() for directory in STORAGE_FORBIDDEN_DIRS):
        # So sánh sau resolve; trên Windows Path so khớp không phân biệt hoa/thường
        if root.is_relative_to(forbidden) or forbidden.is_relative_to(root):
            raise ValueError(
                f"storage_root không được nằm trong hoặc bao trùm thư mục {forbidden.name}/"
            )
    return root


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "AI Video Hub"
    app_version: str = "0.1.0"
    app_env: AppEnvironment = "development"
    debug: bool = False

    # Bắt buộc, không có default: thiếu thì app dừng ngay khi khởi động
    database_url: str

    allowed_origins: list[str] = ["http://localhost:8000", "http://127.0.0.1:8000"]
    allow_credentials: bool = True
    # URL gốc mà người dùng truy cập — dùng để dựng link trong email
    public_base_url: str = "http://localhost:8000"

    # --- Phiên đăng nhập (cookie) ---
    session_cookie_name: str = "avh_session"
    csrf_cookie_name: str = "avh_csrf"
    # Cookie "thiết bị quen" (sau một lần đăng nhập thành công): có ngân sách đăng nhập riêng nên
    # kẻ tấn công không khóa được chủ tài khoản trên trình duyệt quen
    device_cookie_name: str = "avh_device"
    trusted_device_ttl_days: int = Field(default=30, ge=1, le=365)
    csrf_header_name: str = "X-CSRF-Token"
    session_ttl_hours: int = Field(default=24 * 7, ge=1, le=24 * 30)
    # Dev chạy http://localhost nên mặc định False; production bắt buộc True (validator bên dưới)
    cookie_secure: bool = False

    # --- Đặt lại mật khẩu & email ---
    password_reset_ttl_minutes: int = Field(default=30, ge=5, le=60)
    email_backend: EmailBackend = "file"
    email_outbox_dir: str = "storage/dev-outbox"

    # --- Lưu trữ file người dùng (P2-3) ---
    # Thư mục gốc chứa uploads/, videos/, posters/, tmp/; đã git-ignore. Thư mục được tạo khi ghi
    # file đầu tiên, không phải lúc đọc cấu hình
    storage_root: str = "storage"
    storage_quota_bytes_per_user: int = Field(default=DEFAULT_STORAGE_QUOTA_BYTES, gt=0)

    # --- Giới hạn kích thước (P2-4) ---
    # Ảnh upload (byte thật đọc được) và ảnh sau khi mã hóa lại đều không được vượt mức này
    image_upload_max_bytes: int = Field(default=DEFAULT_IMAGE_UPLOAD_MAX_BYTES, gt=0)
    # Tổng body của MỌI request, đếm khi đang nhận (không tin Content-Length) → 413
    max_request_body_bytes: int = Field(default=DEFAULT_MAX_REQUEST_BODY_BYTES, gt=0)

    # --- Reverse proxy ---
    # Rỗng = không có proxy: X-Forwarded-For bị bỏ qua. Có proxy: chỉ khai báo đúng IP/CIDR của
    # proxy (JSON, ví dụ ["10.0.0.5"]). Wildcard ("*", 0.0.0.0/0, ::/0) bị từ chối.
    trusted_proxy_ips: list[str] = []

    # --- Giới hạn truy cập ---
    rate_limit_enabled: bool = True
    # Tổng số bộ đếm tối đa giữ trong RAM (~1,1 KB mỗi bộ đếm), chia cho từng rule theo
    # AUTH_CAPACITY_SHARES. Rule theo IP hết chỗ → client MỚI bị từ chối (fail-closed); rule theo
    # email hết chỗ → email MỚI chỉ còn được bảo vệ bởi rule IP
    rate_limit_max_keys: int = Field(default=100_000, ge=100, le=1_000_000)
    # Khóa HMAC cho bộ đếm theo tài khoản trong database (không lưu email thô). Bắt buộc ở
    # staging/production và phải GIỐNG NHAU giữa mọi worker; dev bỏ trống thì dùng khóa ngẫu nhiên
    auth_throttle_key: SecretStr | None = None

    @field_validator("trusted_proxy_ips")
    @classmethod
    def validate_trusted_proxies(cls, entries: list[str]) -> list[str]:
        parse_trusted_proxies(entries)  # ném ValueError nếu sai định dạng hoặc là wildcard
        return entries

    @cached_property
    def trusted_proxy_networks(self) -> tuple[IPNetwork, ...]:
        return parse_trusted_proxies(self.trusted_proxy_ips)

    @field_validator("storage_root")
    @classmethod
    def validate_storage_root(cls, raw_root: str) -> str:
        resolve_storage_root(raw_root)  # ném ValueError nếu đè lên static/ hoặc app/
        return raw_root

    @cached_property
    def storage_root_path(self) -> Path:
        return resolve_storage_root(self.storage_root)

    @field_validator("public_base_url")
    @classmethod
    def validate_public_base_url(cls, url: str) -> str:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(
                "public_base_url phải là URL http(s) đầy đủ, ví dụ https://example.com"
            )
        return url

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_hardened(self) -> bool:
        """Staging và production: môi trường có người dùng/dữ liệu thật → ép cấu hình an toàn."""
        return self.app_env in HARDENED_ENVIRONMENTS

    @property
    def session_ttl_seconds(self) -> int:
        return self.session_ttl_hours * 3600

    @property
    def trusted_device_ttl_seconds(self) -> int:
        return self.trusted_device_ttl_days * 24 * 3600

    @property
    def throttle_key(self) -> bytes:
        if self.auth_throttle_key is None:
            return _development_throttle_key()
        return self.auth_throttle_key.get_secret_value().encode()

    def _cookie_key(self, name: str) -> str:
        # __Host- chỉ hợp lệ với cookie Secure, Path=/ và không có Domain → khi bật, subdomain
        # không thể ghi đè cookie. Trình duyệt từ chối __Host- qua http → dev giữ tên thường.
        return f"{HOST_COOKIE_PREFIX}{name}" if self.cookie_secure else name

    @property
    def session_cookie_key(self) -> str:
        return self._cookie_key(self.session_cookie_name)

    @property
    def device_cookie_key(self) -> str:
        return self._cookie_key(self.device_cookie_name)

    @model_validator(mode="after")
    def reject_unsafe_combinations(self) -> Self:
        if self.max_request_body_bytes <= self.image_upload_max_bytes:
            raise ValueError(
                "max_request_body_bytes phải lớn hơn image_upload_max_bytes "
                "(còn chỗ cho phần đầu/cuối của multipart)"
            )
        if self.allow_credentials and WILDCARD_ORIGIN in self.allowed_origins:
            raise ValueError(
                "allowed_origins không được chứa '*' khi allow_credentials=True "
                "(trình duyệt sẽ gửi cookie tới mọi origin)"
            )
        if self.is_hardened:
            self._reject_unsafe_hardened_settings()
        return self

    def _reject_unsafe_hardened_settings(self) -> None:
        env = self.app_env
        if self.debug:
            raise ValueError(f"debug phải tắt khi APP_ENV={env}")
        if not self.cookie_secure:
            raise ValueError(f"cookie_secure phải bật khi APP_ENV={env} (chỉ gửi cookie qua HTTPS)")
        if urlsplit(self.public_base_url).scheme != "https":
            raise ValueError(f"public_base_url phải dùng https khi APP_ENV={env}")
        if not self.rate_limit_enabled:
            raise ValueError(f"rate_limit_enabled phải bật khi APP_ENV={env}")
        if self.email_backend in DEVELOPMENT_EMAIL_BACKENDS:
            raise ValueError(
                "email_backend 'file'/'memory' chỉ dành cho dev/test vì lưu cả token reset; "
                f"khi APP_ENV={env} dùng 'disabled' cho tới khi tích hợp nhà cung cấp email thật"
            )
        key = self.auth_throttle_key
        if key is None or len(key.get_secret_value()) < MIN_THROTTLE_KEY_LENGTH:
            raise ValueError(
                f"auth_throttle_key phải có ít nhất {MIN_THROTTLE_KEY_LENGTH} ký tự "
                f"khi APP_ENV={env} (giống nhau giữa mọi worker)"
            )


@lru_cache
def get_settings() -> Settings:
    """Settings đọc từ môi trường thật — chỉ gọi khi dựng app, không gọi lúc import."""
    return Settings()  # database_url đến từ biến môi trường / .env
