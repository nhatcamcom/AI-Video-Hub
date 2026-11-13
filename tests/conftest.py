"""Fixture dùng chung: settings cô lập, app dựng bằng factory, client ASGI không mở cổng mạng."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.core.database import Base
from app.main import create_app

IN_MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

SettingValue = str | bool | int | list[str]
# Khóa HMAC giả cho test (staging/production bắt buộc có khóa ≥ 32 ký tự)
TEST_THROTTLE_KEY = "test-only-throttle-key-0123456789abcdef"


def build_settings(**overrides: SettingValue) -> Settings:
    """Settings cho test: không đọc file .env của máy, email giữ trong bộ nhớ."""
    values: dict[str, SettingValue] = {
        "database_url": IN_MEMORY_DATABASE_URL,
        "app_env": "test",
        "email_backend": "memory",
        **overrides,
    }
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def build_production_settings(**overrides: SettingValue) -> Settings:
    """Cấu hình production hợp lệ tối thiểu (HTTPS, cookie Secure, không dùng hộp thư dev)."""
    production_values: dict[str, SettingValue] = {
        "app_env": "production",
        "cookie_secure": True,
        "email_backend": "disabled",
        "public_base_url": "https://app.example.com",
        "auth_throttle_key": TEST_THROTTLE_KEY,
        **overrides,
    }
    return build_settings(**production_values)


def build_staging_settings(**overrides: SettingValue) -> Settings:
    """Staging được ép các ràng buộc bảo mật như production (kể cả không ghi token reset ra đĩa)."""
    staging_values: dict[str, SettingValue] = {
        "app_env": "staging",
        "cookie_secure": True,
        "email_backend": "disabled",
        "public_base_url": "https://staging.example.com",
        "auth_throttle_key": TEST_THROTTLE_KEY,
        **overrides,
    }
    return build_settings(**staging_values)


def sqlite_file_url(directory: Path) -> str:
    return f"sqlite+aiosqlite:///{(directory / 'test.db').as_posix()}"


async def create_schema(app: FastAPI) -> None:
    async with app.state.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    # File SQLite riêng cho mỗi test: mọi kết nối trong test thấy cùng một DB
    return build_settings(database_url=sqlite_file_url(tmp_path))


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    application = create_app(settings)
    await create_schema(application)
    yield application
    await application.state.engine.dispose()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    # raise_app_exceptions=False: nhận response 500 thật thay vì để exception nổ trong test
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
