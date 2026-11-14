"""P2-1: trang khung + file tĩnh cùng origin, header bảo mật trên MỌI response."""

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient, Response
from starlette.types import Message

from app.core.config import Settings
from app.main import create_app
from tests.conftest import (
    build_production_settings,
    build_settings,
    build_staging_settings,
    create_schema,
)

pytestmark = pytest.mark.integration

PROJECT_ROOT = Path(__file__).resolve().parents[2]

EXPECTED_APP_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
    "media-src 'self' blob:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; "
    "form-action 'self'"
)
EXPECTED_COMMON_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "strict-origin-when-cross-origin",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
}
EXPECTED_HSTS = "max-age=31536000"
SOURCE_MARKER = "def create_app"  # chuỗi chỉ có trong app/main.py — không được lộ qua /static


@pytest.fixture
async def secure_client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    settings = build_settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'secure.db').as_posix()}",
        cookie_secure=True,
    )
    application = create_app(settings)
    await create_schema(application)
    transport = ASGITransport(app=application, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="https://test") as http_client:
        yield http_client
    await application.state.engine.dispose()


def build_development_settings() -> Settings:
    return build_settings(app_env="development")


ENVIRONMENT_SETTINGS: dict[str, Callable[[], Settings]] = {
    "development": build_development_settings,
    "test": build_settings,
    "staging": build_staging_settings,
    "production": build_production_settings,
}
DOCS_DISABLED_ENVIRONMENTS = ["test", "staging", "production"]
HTTPS_ONLY_ENVIRONMENTS = {"staging", "production"}  # cookie_secure bắt buộc → có HSTS
DOCS_PAGES = ["/docs", "/redoc", "/docs/oauth2-redirect"]
DOCS_ENDPOINTS = [*DOCS_PAGES, "/openapi.json"]
DOCS_ENDPOINT_IDS = ["swagger", "redoc", "oauth2", "openapi"]


async def environment_client_get(environment: str, path: str) -> Response:
    """GET trên app dựng với cấu hình của đúng môi trường (staging/production ép HTTPS)."""
    application = create_app(ENVIRONMENT_SETTINGS[environment]())
    transport = ASGITransport(app=application, raise_app_exceptions=False)
    try:
        async with AsyncClient(transport=transport, base_url="https://test") as http_client:
            return await http_client.get(path)
    finally:
        await application.state.engine.dispose()


async def send_raw_get(app: FastAPI, raw_path: str) -> tuple[int, bytes]:
    """Gửi thẳng ASGI scope để đường dẫn KHÔNG bị httpx chuẩn hóa ("..", "\\") trước khi tới app."""
    messages: list[Message] = []
    request_messages = [{"type": "http.request", "body": b"", "more_body": False}]
    client_still_connected = asyncio.Event()  # không bao giờ set: client giữ kết nối như thật

    async def receive() -> Message:
        # Lần sau trả về ngay sẽ làm vòng chờ http.disconnect của FileResponse quay vô hạn
        if request_messages:
            return request_messages.pop()
        await client_still_connected.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": raw_path,
        "raw_path": raw_path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"test")],
        "client": ("127.0.0.1", 50000),
        "server": ("test", 80),
    }
    await app(scope, receive, send)
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return status, body


def assert_strict_security_headers(headers: Mapping[str, str]) -> None:
    lowered = {key.lower(): value for key, value in headers.items()}
    assert lowered["content-security-policy"] == EXPECTED_APP_CSP
    for name, value in EXPECTED_COMMON_HEADERS.items():
        assert lowered[name] == value


# --------------------------------------------------------------------------- trang và file tĩnh
async def test_index_page_is_served_as_html(client: AsyncClient) -> None:
    response = await client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<h1" in response.text


@pytest.mark.parametrize(
    ("path", "expected_content_type"),
    [
        ("/static/css/tokens.css", "text/css"),
        ("/static/css/base.css", "text/css"),
        ("/static/js/api.js", "text/javascript"),
        ("/static/js/home.js", "text/javascript"),
    ],
    ids=["tokens-css", "base-css", "api-js", "home-js"],
)
async def test_static_assets_are_served_with_correct_content_type(
    client: AsyncClient, path: str, expected_content_type: str
) -> None:
    response = await client.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"].split(";")[0] == expected_content_type


async def test_index_page_is_not_listed_in_openapi() -> None:
    schema = (await environment_client_get("development", "/openapi.json")).json()

    assert "/api/v1/health" in schema["paths"]
    assert "/" not in schema["paths"]


# --------------------------------------------------------------------------- header bảo mật
@pytest.mark.parametrize(
    ("method", "path", "expected_status"),
    [
        ("GET", "/", 200),
        ("GET", "/static/css/tokens.css", 200),
        ("GET", "/static/js/api.js", 200),
        ("GET", "/api/v1/health", 200),
        ("GET", "/openapi.json", 404),
        ("GET", "/khong-ton-tai", 404),
        ("GET", "/static/khong-ton-tai.css", 404),
        ("GET", "/api/v1/auth/me", 401),
        ("POST", "/api/v1/health", 405),
    ],
    ids=[
        "index",
        "static-css",
        "static-js",
        "api-health",
        "openapi-disabled-404",
        "unknown-route-404",
        "missing-static-404",
        "unauthenticated-401",
        "wrong-method-405",
    ],
)
async def test_every_response_has_strict_security_headers(
    client: AsyncClient, method: str, path: str, expected_status: int
) -> None:
    response = await client.request(method, path)

    assert response.status_code == expected_status
    assert_strict_security_headers(response.headers)
    assert "strict-transport-security" not in response.headers


async def test_unexpected_500_error_still_has_security_headers(
    app: FastAPI, client: AsyncClient
) -> None:
    probe = APIRouter()

    @probe.get("/api/v1/_probe/crash")
    async def crash() -> None:
        raise RuntimeError("loi-noi-bo")

    app.include_router(probe)

    response = await client.get("/api/v1/_probe/crash")

    assert response.status_code == 500
    assert_strict_security_headers(response.headers)


async def test_cors_preflight_response_has_security_headers(client: AsyncClient) -> None:
    response = await client.options(
        "/api/v1/health",
        headers={"Origin": "http://localhost:8000", "Access-Control-Request-Method": "GET"},
    )

    assert response.status_code == 200
    assert_strict_security_headers(response.headers)


async def test_app_csp_never_allows_inline_or_eval(client: AsyncClient) -> None:
    csp = (await client.get("/")).headers["content-security-policy"]

    assert "unsafe-inline" not in csp
    assert "unsafe-eval" not in csp


@pytest.mark.parametrize(
    "path", ["/", "/api/v1/health", "/khong-ton-tai"], ids=["index", "api", "not-found"]
)
async def test_hsts_is_sent_when_cookie_secure_is_enabled(
    secure_client: AsyncClient, path: str
) -> None:
    response = await secure_client.get(path)

    assert response.headers["strict-transport-security"] == EXPECTED_HSTS
    assert_strict_security_headers(response.headers)


# --------------------------------------------------------------------------- path traversal
@pytest.mark.parametrize(
    "raw_path",
    [
        "/static/../app/main.py",
        "/static/../../app/main.py",
        "/static/css/../../app/main.py",
        "/static/..\\app\\main.py",
        "/static/css\\..\\..\\app\\main.py",
        "/static/../.env.example",
        "/static/../storage/dev-outbox",
        "/static/../pyproject.toml",
    ],
    ids=[
        "dot-dot",
        "double-dot-dot",
        "nested-dot-dot",
        "backslash",
        "nested-backslash",
        "env-example",
        "storage-dir",
        "pyproject",
    ],
)
async def test_static_path_traversal_is_rejected(app: FastAPI, raw_path: str) -> None:
    status, body = await send_raw_get(app, raw_path)

    assert status == 404
    assert SOURCE_MARKER.encode() not in body
    assert b"DATABASE_URL" not in body


@pytest.mark.parametrize(
    "path",
    [
        "/static/%2e%2e/app/main.py",
        "/static/%2E%2E/%2E%2E/app/main.py",
        "/static/..%2fapp%2fmain.py",
        "/static/..%5capp%5cmain.py",
        "/static/%2e%2e%2f.env.example",
    ],
    ids=["encoded-dots", "encoded-dots-upper", "encoded-slash", "encoded-backslash", "encoded-env"],
)
async def test_encoded_static_path_traversal_is_rejected(client: AsyncClient, path: str) -> None:
    response = await client.get(path)

    assert response.status_code == 404
    assert SOURCE_MARKER not in response.text
    assert_strict_security_headers(response.headers)


@pytest.mark.parametrize("path", ["/static", "/static/", "/static/css", "/static/css/"])
async def test_static_directories_are_not_listed(client: AsyncClient, path: str) -> None:
    response = await client.get(path, follow_redirects=True)

    assert response.status_code == 404


async def test_raw_request_helper_serves_valid_static_file(app: FastAPI) -> None:
    # Đối chứng: cùng cách gửi thô, đường dẫn hợp lệ trả 200 → các ca 404 ở trên là do bị chặn
    status, body = await send_raw_get(app, "/static/css/tokens.css")

    assert status == 200
    assert b":root" in body


def test_traversal_targets_exist_so_404_proves_blocking() -> None:
    assert (PROJECT_ROOT / "app" / "main.py").is_file()
    assert (PROJECT_ROOT / ".env.example").is_file()
    assert SOURCE_MARKER in (PROJECT_ROOT / "app" / "main.py").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- API không bị che
async def test_api_routes_are_not_shadowed_by_static_or_index(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["status"] == "ok"


async def test_unknown_api_route_still_returns_json_envelope(client: AsyncClient) -> None:
    response = await client.get("/api/v1/khong-ton-tai")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# --------------------------------------------------------------------------- /docs, /redoc
@pytest.mark.parametrize("path", DOCS_PAGES, ids=["swagger", "redoc", "oauth2"])
async def test_docs_pages_are_enabled_in_development_with_docs_csp(path: str) -> None:
    response = await environment_client_get("development", path)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    csp = response.headers["content-security-policy"]
    assert csp != EXPECTED_APP_CSP
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp
    assert "unsafe-eval" not in csp
    assert "strict-transport-security" not in response.headers
    for name, value in EXPECTED_COMMON_HEADERS.items():
        assert response.headers[name] == value


async def test_openapi_schema_is_served_in_development_with_strict_csp() -> None:
    response = await environment_client_get("development", "/openapi.json")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert_strict_security_headers(response.headers)


@pytest.mark.parametrize(
    "path",
    ["/docs/", "/docsx", "/docs/../", "/static/docs", "/api/v1/docs"],
    ids=["trailing-slash", "prefix-lookalike", "dot-dot", "static-docs", "api-docs"],
)
async def test_docs_csp_exception_applies_only_to_exact_docs_paths(path: str) -> None:
    # Chạy ở development (docs đang bật) để chứng minh ngoại lệ không lan sang đường dẫn khác
    response = await environment_client_get("development", path)

    assert response.headers["content-security-policy"] == EXPECTED_APP_CSP


@pytest.mark.parametrize("environment", DOCS_DISABLED_ENVIRONMENTS)
@pytest.mark.parametrize("path", DOCS_ENDPOINTS, ids=DOCS_ENDPOINT_IDS)
async def test_docs_are_disabled_outside_development_with_strict_csp(
    environment: str, path: str
) -> None:
    response = await environment_client_get(environment, path)

    assert response.status_code == 404
    assert_strict_security_headers(response.headers)
    if environment in HTTPS_ONLY_ENVIRONMENTS:
        assert response.headers["strict-transport-security"] == EXPECTED_HSTS
    else:
        assert "strict-transport-security" not in response.headers


@pytest.mark.parametrize("environment", list(ENVIRONMENT_SETTINGS))
async def test_index_page_is_served_with_strict_csp_in_every_environment(
    environment: str,
) -> None:
    response = await environment_client_get(environment, "/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert_strict_security_headers(response.headers)
