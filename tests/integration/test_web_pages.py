"""P2-2: các trang giao diện xác thực được phục vụ theo danh sách trắng cố định."""

from collections.abc import AsyncIterator, Mapping

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from tests.conftest import build_settings

pytestmark = pytest.mark.integration

EXPECTED_APP_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
    "media-src 'self' blob:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; "
    "form-action 'self'"
)

# Đường dẫn → chuỗi đánh dấu chỉ có trong đúng trang đó
AUTH_PAGES: Mapping[str, str] = {
    "/login": 'id="login-form"',
    "/register": 'id="register-form"',
    "/forgot-password": 'id="forgot-password-form"',
    # Link trong email Phase 1 trỏ đúng tới đường dẫn này (app/modules/auth/emails.py)
    "/reset-password": 'id="reset-password-form"',
}


@pytest.fixture
async def development_app() -> AsyncIterator[FastAPI]:
    application = create_app(build_settings(app_env="development"))
    yield application
    await application.state.engine.dispose()


@pytest.mark.parametrize(("path", "marker"), list(AUTH_PAGES.items()), ids=list(AUTH_PAGES))
async def test_auth_pages_are_served_as_html_with_strict_csp(
    client: AsyncClient, path: str, marker: str
) -> None:
    response = await client.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert marker in response.text
    assert response.headers["content-security-policy"] == EXPECTED_APP_CSP
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_reset_password_page_ignores_query_and_serves_same_html(client: AsyncClient) -> None:
    plain = await client.get("/reset-password")
    with_query = await client.get("/reset-password?utm=email")

    assert with_query.status_code == 200
    assert with_query.text == plain.text


async def test_index_page_is_still_served(client: AsyncClient) -> None:
    response = await client.get("/")

    assert response.status_code == 200
    assert 'id="server-status"' in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/login.html",
        "/register.html",
        "/index.html",
        "/admin",
        "/reset-password.html",
        "/reset-password/extra",
        "/Login",
    ],
    ids=["login-html", "register-html", "index-html", "admin", "reset-html", "nested", "case"],
)
async def test_paths_outside_page_whitelist_return_404(client: AsyncClient, path: str) -> None:
    response = await client.get(path)

    assert response.status_code == 404
    assert response.headers["content-security-policy"] == EXPECTED_APP_CSP


@pytest.mark.parametrize("path", list(AUTH_PAGES))
async def test_posting_to_page_path_is_rejected_without_echoing_body(
    client: AsyncClient, path: str
) -> None:
    # Nếu JS không chạy, form method="post" gửi về chính trang: phải bị từ chối, không lưu gì
    response = await client.post(
        path, data={"email": "user@example.com", "password": "khong-duoc-hien-thi"}
    )

    assert response.status_code == 405
    assert "khong-duoc-hien-thi" not in response.text


async def test_auth_pages_are_not_listed_in_openapi(development_app: FastAPI) -> None:
    transport = ASGITransport(app=development_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        schema = (await http_client.get("/openapi.json")).json()

    assert "/api/v1/auth/login" in schema["paths"]
    for path in ["/", *AUTH_PAGES]:
        assert path not in schema["paths"]


async def test_auth_api_is_not_shadowed_by_pages(client: AsyncClient) -> None:
    response = await client.get("/api/v1/auth/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
