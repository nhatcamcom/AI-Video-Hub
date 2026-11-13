"""App factory. Chạy: `python -m app.server` (tắt proxy_headers của uvicorn; xem app/server.py)."""

import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import Settings, get_settings
from app.core.database import create_engine, create_session_factory
from app.core.email import create_email_sender
from app.core.errors import register_exception_handlers
from app.core.rate_limit import (
    InMemoryRateLimiter,
    NoopRateLimiter,
    RateLimiter,
    allocate_capacity,
)
from app.modules.auth.rate_limits import AUTH_CAPACITY_SHARES
from app.modules.auth.router import router as auth_router
from app.modules.auth.security import prepare_password_hashing
from app.modules.health.router import router as health_router
from app.modules.users.router import router as users_router

API_V1_PREFIX = "/api/v1"
CORS_ALLOWED_METHODS = ["GET", "POST", "PATCH", "DELETE", "OPTIONS"]
CORS_BASE_HEADERS = ["Authorization", "Content-Type"]


def create_rate_limiter(
    settings: Settings, clock: Callable[[], float] = time.monotonic
) -> RateLimiter:
    """Limiter của app: mỗi rule auth có sức chứa riêng, chia từ RATE_LIMIT_MAX_KEYS."""
    if not settings.rate_limit_enabled:
        return NoopRateLimiter()
    return InMemoryRateLimiter(
        clock=clock,
        max_tracked_keys=settings.rate_limit_max_keys,
        reserved_capacity=allocate_capacity(settings.rate_limit_max_keys, AUTH_CAPACITY_SHARES),
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()
    engine = create_engine(app_settings)
    prepare_password_hashing()  # một lần mỗi tiến trình (có cache)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await engine.dispose()  # đóng pool kết nối khi tắt app

    # Ẩn tài liệu API trên production để không lộ bề mặt tấn công
    docs_enabled = not app_settings.is_production
    app = FastAPI(
        title=app_settings.app_name,
        version=app_settings.app_version,
        debug=app_settings.debug,
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )
    app.state.settings = app_settings
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.state.rate_limiter = create_rate_limiter(app_settings)
    app.state.email_sender = create_email_sender(app_settings)

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
    return app
