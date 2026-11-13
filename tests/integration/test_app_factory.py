import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.rate_limit import InMemoryRateLimiter, NoopRateLimiter
from app.main import create_app
from tests.conftest import build_production_settings, build_settings

pytestmark = pytest.mark.integration

ALLOWED_ORIGIN = "http://localhost:8000"


async def test_openapi_docs_available_outside_production(client: AsyncClient) -> None:
    assert (await client.get("/docs")).status_code == 200
    schema = (await client.get("/openapi.json")).json()
    assert "/api/v1/health" in schema["paths"]


async def test_openapi_docs_disabled_in_production() -> None:
    app = create_app(build_production_settings())
    transport = ASGITransport(app=app, raise_app_exceptions=False)

    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            docs_status = (await client.get("/docs")).status_code
            openapi_status = (await client.get("/openapi.json")).status_code
    finally:
        await app.state.engine.dispose()

    assert docs_status == 404
    assert openapi_status == 404


async def test_cors_allows_configured_origin(client: AsyncClient) -> None:
    response = await client.options(
        "/api/v1/health",
        headers={"Origin": ALLOWED_ORIGIN, "Access-Control-Request-Method": "GET"},
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN


async def test_cors_allows_csrf_header_from_configured_origin(client: AsyncClient) -> None:
    response = await client.options(
        "/api/v1/users/me",
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "PATCH",
            "Access-Control-Request-Headers": "content-type,x-csrf-token",
        },
    )

    assert response.status_code == 200
    assert "x-csrf-token" in response.headers["access-control-allow-headers"].lower()


async def test_cors_rejects_unknown_origin(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health", headers={"Origin": "https://evil.example.com"})

    assert "access-control-allow-origin" not in response.headers


def test_rate_limiter_follows_settings() -> None:
    enabled = create_app(build_settings(rate_limit_max_keys=500))
    disabled = create_app(build_settings(rate_limit_enabled=False))

    assert isinstance(enabled.state.rate_limiter, InMemoryRateLimiter)
    assert isinstance(disabled.state.rate_limiter, NoopRateLimiter)


async def test_lifespan_runs_and_disposes_engine_cleanly(app: FastAPI) -> None:
    async with app.router.lifespan_context(app):
        assert app.state.engine is not None
    # Sau khi tắt, engine đã dispose — tạo kết nối mới vẫn được (pool tự khởi tạo lại)
    async with app.state.engine.connect() as connection:
        assert connection is not None
