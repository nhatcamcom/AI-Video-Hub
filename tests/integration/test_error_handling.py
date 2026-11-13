"""Mọi lỗi — của ứng dụng, của framework hay ngoài dự kiến — đều trả về cùng một envelope JSON."""

import logging
from typing import Annotated

import pytest
from fastapi import APIRouter, FastAPI, Query
from httpx import AsyncClient

from app.core.database import DbDep
from app.core.errors import NotFoundError, RateLimitError

pytestmark = pytest.mark.integration

LEAKY_INTERNAL_DETAIL = "chi-tiet-noi-bo-khong-duoc-lo"


@pytest.fixture
def probe_app(app: FastAPI) -> FastAPI:
    """Gắn các route thăm dò chỉ dùng trong test để kích hoạt từng loại lỗi."""
    probe = APIRouter(prefix="/_probe")

    @probe.get("/not-found")
    async def raise_not_found() -> None:
        raise NotFoundError("Video", 42)

    @probe.get("/rate-limited")
    async def raise_rate_limited() -> None:
        raise RateLimitError(retry_after_seconds=30)

    @probe.get("/items")
    async def list_items(limit: Annotated[int, Query(ge=1, le=100)]) -> dict[str, int]:
        return {"limit": limit}

    @probe.get("/crash")
    async def crash() -> None:
        raise RuntimeError(LEAKY_INTERNAL_DETAIL)

    @probe.get("/db-then-fail")
    async def fail_inside_db_session(db: DbDep) -> None:
        raise NotFoundError("Video", 7)

    app.include_router(probe)
    return app


async def test_app_error_returns_standard_envelope(probe_app: FastAPI, client: AsyncClient) -> None:
    response = await client.get("/_probe/not-found")

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert "Video" in body["error"]["message"]
    assert "details" not in body["error"]


async def test_rate_limit_error_includes_retry_after(
    probe_app: FastAPI, client: AsyncClient
) -> None:
    response = await client.get("/_probe/rate-limited")

    assert response.status_code == 429
    assert response.headers["retry-after"] == "30"
    assert response.json()["error"]["code"] == "rate_limit_exceeded"


async def test_unknown_route_returns_not_found_envelope(client: AsyncClient) -> None:
    response = await client.get("/api/v1/khong-ton-tai")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_wrong_method_returns_method_not_allowed_envelope(client: AsyncClient) -> None:
    response = await client.post("/api/v1/health")

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"
    assert "GET" in response.headers["allow"]


@pytest.mark.parametrize("limit", ["0", "101", "abc"], ids=["below-min", "above-max", "not-int"])
async def test_invalid_query_returns_validation_envelope_with_field_details(
    probe_app: FastAPI, client: AsyncClient, limit: str
) -> None:
    response = await client.get("/_probe/items", params={"limit": limit})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"][0]["field"] == "query.limit"
    assert error["details"][0]["message"]


async def test_valid_query_passes_validation(probe_app: FastAPI, client: AsyncClient) -> None:
    response = await client.get("/_probe/items", params={"limit": "20"})

    assert response.status_code == 200
    assert response.json() == {"limit": 20}


async def test_unexpected_error_returns_generic_500_without_leaking_details(
    probe_app: FastAPI, client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR):
        response = await client.get("/_probe/crash")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert LEAKY_INTERNAL_DETAIL not in response.text  # không lộ chi tiết ra client
    assert LEAKY_INTERNAL_DETAIL in caplog.text  # nhưng vẫn được ghi log ở server


async def test_error_inside_db_session_still_returns_envelope(
    probe_app: FastAPI, client: AsyncClient
) -> None:
    response = await client.get("/_probe/db-then-fail")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
