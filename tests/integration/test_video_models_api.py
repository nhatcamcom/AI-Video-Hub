"""GET /api/v1/video-models (P2-5): chỉ model đang bật, đúng thứ tự cấu hình, đúng 6 trường."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.modules.videos.catalog import (
    MOCK_VIDEO_MODEL,
    AspectRatio,
    VideoModelSpec,
    build_video_model_catalog,
)
from tests.conftest import build_settings, create_schema, sqlite_file_url
from tests.helpers import csrf_headers, register_and_login

pytestmark = pytest.mark.integration

URL = "/api/v1/video-models"
PUBLIC_FIELDS = {
    "key",
    "display_name",
    "is_demo",
    "aspect_ratios",
    "durations_seconds",
    "supports_input_image",
}
EXPECTED_MOCK = {
    "key": "mock",
    "display_name": MOCK_VIDEO_MODEL.display_name,
    "is_demo": True,
    "aspect_ratios": ["16:9", "9:16", "1:1"],
    "durations_seconds": [5, 10],
    "supports_input_image": True,
}
FAKE_HIDDEN = VideoModelSpec(
    key="fake-hidden",
    display_name="Fake Hidden",
    is_demo=True,
    aspect_ratios=(AspectRatio.SQUARE,),
    durations_seconds=(5,),
    supports_input_image=False,
)
FAKE_SHOWN = VideoModelSpec(
    key="fake-shown",
    display_name="Fake Shown",
    is_demo=False,
    aspect_ratios=(AspectRatio.PORTRAIT,),
    durations_seconds=(10,),
    supports_input_image=False,
)


@pytest.fixture
async def logged_in(client: AsyncClient) -> AsyncClient:
    await register_and_login(client)
    return client


async def test_logged_in_user_gets_enabled_models(logged_in: AsyncClient) -> None:
    response = await logged_in.get(URL)

    assert response.status_code == 200
    assert response.json() == {"total": 1, "items": [EXPECTED_MOCK]}


async def test_items_expose_exactly_the_approved_fields(logged_in: AsyncClient) -> None:
    [item] = (await logged_in.get(URL)).json()["items"]

    assert set(item) == PUBLIC_FIELDS


async def test_anonymous_request_is_unauthorized(client: AsyncClient) -> None:
    response = await client.get(URL)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert "mock" not in response.text


async def test_response_carries_security_headers(logged_in: AsyncClient) -> None:
    response = await logged_in.get(URL)

    assert response.headers["content-security-policy"].startswith("default-src 'self'")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"


@pytest.mark.parametrize("method", ["POST", "PATCH", "DELETE", "PUT"])
async def test_write_methods_are_not_allowed(logged_in: AsyncClient, method: str) -> None:
    response = await logged_in.request(method, URL, headers=csrf_headers(logged_in))

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"


async def test_only_enabled_models_are_listed_in_configuration_order(
    app: FastAPI, logged_in: AsyncClient
) -> None:
    app.state.video_model_catalog = build_video_model_catalog(
        ["fake-shown", "mock"], known=(MOCK_VIDEO_MODEL, FAKE_HIDDEN, FAKE_SHOWN)
    )

    body = (await logged_in.get(URL)).json()

    assert body["total"] == 2
    assert [item["key"] for item in body["items"]] == ["fake-shown", "mock"]
    assert body["items"][0] == {
        "key": "fake-shown",
        "display_name": "Fake Shown",
        "is_demo": False,
        "aspect_ratios": ["9:16"],
        "durations_seconds": [10],
        "supports_input_image": False,
    }


# --------------------------------------------------------------------------- OpenAPI
@pytest.fixture
async def development_app(tmp_path: Path) -> AsyncIterator[FastAPI]:
    application = create_app(
        build_settings(
            database_url=sqlite_file_url(tmp_path),
            storage_root=str(tmp_path / "storage"),
            app_env="development",
        )
    )
    await create_schema(application)
    yield application
    application.state.image_gate.close()
    await application.state.engine.dispose()


async def test_openapi_documents_response_model_and_401(development_app: FastAPI) -> None:
    transport = ASGITransport(app=development_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        spec = (await client.get("/openapi.json")).json()

    operation = spec["paths"][URL]["get"]
    assert set(operation) >= {"summary", "responses"}
    assert "requestBody" not in operation
    ok_schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert ok_schema == {"$ref": "#/components/schemas/VideoModelListResponse"}
    assert "401" in operation["responses"]
    schemas = spec["components"]["schemas"]
    assert set(schemas["VideoModelListResponse"]["properties"]) == {"total", "items"}
    assert set(schemas["VideoModelResponse"]["properties"]) == PUBLIC_FIELDS
