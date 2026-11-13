from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.main import create_app
from tests.conftest import build_settings

pytestmark = pytest.mark.integration


async def test_health_reports_ok_with_database_status(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "app": "AI Video Hub",
        "version": "0.1.0",
        "environment": "test",
        "database": "ok",
    }


async def test_health_returns_503_when_database_is_unreachable(tmp_path: Path) -> None:
    # Thư mục cha không tồn tại → SQLite không thể mở file DB
    unreachable_url = f"sqlite+aiosqlite:///{(tmp_path / 'missing' / 'app.db').as_posix()}"
    app = create_app(build_settings(database_url=unreachable_url))
    transport = ASGITransport(app=app, raise_app_exceptions=False)

    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/v1/health")
    finally:
        await app.state.engine.dispose()

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "database_unavailable"
    assert "missing" not in response.text  # không lộ đường dẫn / lỗi DB nội bộ


async def test_sqlite_connections_enforce_foreign_keys(app: FastAPI) -> None:
    # SQLite mặc định TẮT ràng buộc khóa ngoại; app phải bật cho mọi kết nối
    async with app.state.session_factory() as session:
        result = await session.execute(text("PRAGMA foreign_keys"))

    assert result.scalar_one() == 1
