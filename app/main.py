"""App factory. Chạy: `python -m app.server` (tắt proxy_headers của uvicorn; xem app/server.py)."""

import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.body_limit import RequestBodyLimitMiddleware

from app.core.config import AppEnvironment, Settings, get_settings
from app.core.database import create_engine, create_session_factory
from app.core.email import create_email_sender
from app.core.errors import PayloadTooLargeEnvelopeMiddleware, register_exception_handlers
from app.core.rate_limit import (
    InMemoryRateLimiter,
    NoopRateLimiter,
    RateLimiter,
    RateLimitRule,
    allocate_capacity,
)
from app.core.storage import create_storage
from app.modules.auth.rate_limits import AUTH_CAPACITY_SHARES
from app.modules.auth.router import router as auth_router
from app.modules.auth.security import prepare_password_hashing
from app.modules.health.router import router as health_router
from app.modules.media.image_processing import ImageProcessingGate
from app.modules.media.rate_limits import MEDIA_CAPACITY_SHARES
from app.modules.media.router import router as media_router
from app.modules.users.router import router as users_router
from app.modules.videos.catalog import build_video_model_catalog
from app.modules.videos.router import router as video_models_router
from app.web.router import mount_static_files
from app.web.router import router as web_router
from app.web.security_headers import SecureHeadersFastAPI, SecurityHeadersPolicy

API_V1_PREFIX = "/api/v1"
DOCS_ENVIRONMENT: AppEnvironment = "development"
DOCS_URL = "/docs"
REDOC_URL = "/redoc"
SWAGGER_OAUTH2_REDIRECT_URL = "/docs/oauth2-redirect"
OPENAPI_URL = "/openapi.json"
CORS_ALLOWED_METHODS = ["GET", "POST", "PATCH", "DELETE", "OPTIONS"]
CORS_BASE_HEADERS = ["Authorization", "Content-Type"]
# Mọi rule IP in-memory của app, mỗi rule một phần sức chứa riêng (tỉ trọng, không cần tổng 100)
APP_CAPACITY_SHARES: dict[RateLimitRule, int] = {**AUTH_CAPACITY_SHARES, **MEDIA_CAPACITY_SHARES}


def create_rate_limiter(
    settings: Settings, clock: Callable[[], float] = time.monotonic
) -> RateLimiter:
    """Limiter của app: mỗi rule (auth, media) có sức chứa riêng, chia từ RATE_LIMIT_MAX_KEYS."""
    if not settings.rate_limit_enabled:
        return NoopRateLimiter()
    return InMemoryRateLimiter(
        clock=clock,
        max_tracked_keys=settings.rate_limit_max_keys,
        reserved_capacity=allocate_capacity(settings.rate_limit_max_keys, APP_CAPACITY_SHARES),
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()
    engine = create_engine(app_settings)
    prepare_password_hashing()  # một lần mỗi tiến trình (có cache)
    # Key model lạ → dừng ngay khi khởi động, không đợi tới request đầu tiên
    video_model_catalog = build_video_model_catalog(app_settings.enabled_video_models)
    image_gate = ImageProcessingGate()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        image_gate.close()  # dừng thread pool xử lý ảnh
        await engine.dispose()  # đóng pool kết nối khi tắt app

    # Tài liệu API (/docs, /redoc, /docs/oauth2-redirect, /openapi.json) CHỈ bật ở development;
    # test, staging, production trả 404 và không áp dụng ngoại lệ CSP của trang docs
    docs_enabled = app_settings.app_env == DOCS_ENVIRONMENT
    docs_pages = frozenset({DOCS_URL, REDOC_URL, SWAGGER_OAUTH2_REDIRECT_URL})
    app = SecureHeadersFastAPI(
        security_headers_policy=SecurityHeadersPolicy(
            is_hsts_enabled=app_settings.cookie_secure,  # cookie Secure ⇔ chạy sau HTTPS
            docs_paths=docs_pages if docs_enabled else frozenset(),
        ),
        title=app_settings.app_name,
        version=app_settings.app_version,
        debug=app_settings.debug,
        lifespan=lifespan,
        docs_url=DOCS_URL if docs_enabled else None,
        redoc_url=REDOC_URL if docs_enabled else None,
        swagger_ui_oauth2_redirect_url=SWAGGER_OAUTH2_REDIRECT_URL,
        openapi_url=OPENAPI_URL if docs_enabled else None,
    )
    app.state.settings = app_settings
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.state.rate_limiter = create_rate_limiter(app_settings)
    app.state.email_sender = create_email_sender(app_settings)
    app.state.storage = create_storage(app_settings)
    app.state.image_gate = image_gate
    app.state.video_model_catalog = video_model_catalog

    # Thứ tự: add_middleware sau cùng nằm NGOÀI cùng. Giới hạn body đếm byte thật khi đang nhận;
    # middleware chuẩn hóa phải bọc ngoài nó để đổi 413 text thuần thành envelope JSON
    app.add_middleware(
        RequestBodyLimitMiddleware, max_body_size=app_settings.max_request_body_bytes
    )
    app.add_middleware(PayloadTooLargeEnvelopeMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=app_settings.allowed_origins,
        allow_credentials=app_settings.allow_credentials,
        allow_methods=CORS_ALLOWED_METHODS,
        allow_headers=[*CORS_BASE_HEADERS, app_settings.csrf_header_name],
    )
    register_exception_handlers(app)
    app.include_router(health_router, prefix=API_V1_PREFIX)
    app.include_router(auth_router, prefix=API_V1_PREFIX)
    app.include_router(users_router, prefix=API_V1_PREFIX)
    app.include_router(media_router, prefix=API_V1_PREFIX)
    app.include_router(video_models_router, prefix=API_V1_PREFIX)
    app.include_router(web_router)
    mount_static_files(app)
    return app
