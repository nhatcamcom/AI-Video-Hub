"""Header bảo mật cho MỌI response (trang HTML, file tĩnh, API, lỗi 4xx/5xx).

Middleware được đặt NGOÀI toàn bộ middleware stack của FastAPI (xem `SecureHeadersFastAPI`):
Starlette luôn để `ServerErrorMiddleware` ngoài cùng, nên middleware thêm bằng `add_middleware`
không chạm tới response 500. Bọc bên ngoài thì cả response 500 cũng mang header.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# CSP của ứng dụng: chỉ tài nguyên cùng origin, KHÔNG có 'unsafe-inline' / 'unsafe-eval'.
# blob: cho img/media để xem trước ảnh/video chọn từ máy người dùng (P2-10) mà không cần upload.
APP_CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data: blob:",
        "media-src 'self' blob:",
        "object-src 'none'",
        "base-uri 'self'",
        "frame-ancestors 'none'",
        "form-action 'self'",
    ]
)

# Ngoại lệ CHỈ cho trang tài liệu API (/docs, /redoc, /docs/oauth2-redirect), vốn chỉ bật khi
# APP_ENV=development. Swagger UI / ReDoc của FastAPI tải script, CSS từ CDN và dùng
# script/style inline.
# Không có 'unsafe-eval'; vẫn cấm nhúng frame, plugin và đổi <base>.
DOCS_CDN = "https://cdn.jsdelivr.net"
DOCS_CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        f"script-src 'self' 'unsafe-inline' {DOCS_CDN}",
        f"style-src 'self' 'unsafe-inline' {DOCS_CDN} https://fonts.googleapis.com",
        "font-src 'self' https://fonts.gstatic.com",
        "img-src 'self' data: https://fastapi.tiangolo.com https://cdn.redoc.ly",
        "worker-src 'self' blob:",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "frame-ancestors 'none'",
        "form-action 'self'",
    ]
)

COMMON_SECURITY_HEADERS: Mapping[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}

# Một năm. Không thêm includeSubDomains/preload: chưa biết các subdomain khác của tên miền
# production đã có HTTPS hay chưa, mà HSTS rất khó thu hồi khi trình duyệt đã ghi nhớ.
HSTS_MAX_AGE_SECONDS = 365 * 24 * 60 * 60
HSTS_HEADER_VALUE = f"max-age={HSTS_MAX_AGE_SECONDS}"


@dataclass(frozen=True)
class SecurityHeadersPolicy:
    """Header áp cho từng đường dẫn.

    `docs_paths` rỗng khi tài liệu API bị tắt (mọi môi trường ngoài development).
    """

    is_hsts_enabled: bool
    docs_paths: frozenset[str] = field(default_factory=frozenset)

    def headers_for(self, path: str) -> dict[str, str]:
        # So khớp CHÍNH XÁC: "/docs/", "/docsx", "/static/docs" vẫn nhận CSP chặt
        is_docs_page = path in self.docs_paths
        csp = DOCS_CONTENT_SECURITY_POLICY if is_docs_page else APP_CONTENT_SECURITY_POLICY
        headers = {**COMMON_SECURITY_HEADERS, "Content-Security-Policy": csp}
        if self.is_hsts_enabled:
            headers["Strict-Transport-Security"] = HSTS_HEADER_VALUE
        return headers


class SecurityHeadersMiddleware:
    """ASGI middleware thuần (không dùng BaseHTTPMiddleware): không đệm body, không đổi luồng."""

    def __init__(self, app: ASGIApp, policy: SecurityHeadersPolicy) -> None:
        self.app = app
        self.policy = policy

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        security_headers = self.policy.headers_for(scope["path"])

        async def send_with_security_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                # Ghi đè có chủ đích: một route không thể vô tình nới lỏng chính sách chung
                for name, value in security_headers.items():
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_security_headers)


class SecureHeadersFastAPI(FastAPI):
    """FastAPI bọc `SecurityHeadersMiddleware` ở lớp ngoài cùng của middleware stack."""

    def __init__(self, *, security_headers_policy: SecurityHeadersPolicy, **kwargs: Any) -> None:
        self.security_headers_policy = security_headers_policy
        super().__init__(**kwargs)

    def build_middleware_stack(self) -> ASGIApp:
        return SecurityHeadersMiddleware(
            super().build_middleware_stack(), self.security_headers_policy
        )
