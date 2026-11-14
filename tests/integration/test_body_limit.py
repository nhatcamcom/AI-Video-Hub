"""Giới hạn body toàn app (P2-4): mọi 413 là envelope JSON `payload_too_large` + header bảo mật."""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from starlette.types import Message

from app.main import create_app
from tests.conftest import build_settings, create_schema, sqlite_file_url
from tests.helpers import CSRF_HEADER, csrf_headers, register_and_login

pytestmark = pytest.mark.integration

BODY_LIMIT = 4 * 1024
CHUNK = 1024
EXPECTED_CSP_START = "default-src 'self'"


@pytest.fixture
async def limited_app(tmp_path: Path) -> AsyncIterator[FastAPI]:
    application = create_app(
        build_settings(
            database_url=sqlite_file_url(tmp_path),
            storage_root=str(tmp_path / "storage"),
            image_upload_max_bytes=2 * 1024,
            max_request_body_bytes=BODY_LIMIT,
        )
    )
    yield application
    await application.state.engine.dispose()


async def send_raw(
    app: FastAPI,
    path: str,
    chunks: list[bytes],
    headers: list[tuple[bytes, bytes]],
    method: str = "POST",
) -> tuple[int, dict[str, str], bytes, int]:
    """Gửi ASGI thô (không qua httpx) để tự kiểm soát Content-Length và cách chia mảnh body."""
    pending = list(chunks)
    received = 0
    messages: list[Message] = []
    finished = asyncio.Event()

    async def receive() -> Message:
        nonlocal received
        if not pending:
            await finished.wait()
            return {"type": "http.disconnect"}
        received += 1
        chunk = pending.pop(0)
        return {"type": "http.request", "body": chunk, "more_body": bool(pending)}

    async def send(message: Message) -> None:
        messages.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body"):
            finished.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"test"), *headers],
        "client": ("127.0.0.1", 50000),
        "server": ("test", 80),
    }
    await app(scope, receive, send)
    start = next(m for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    response_headers = {key.decode().lower(): value.decode() for key, value in start["headers"]}
    return start["status"], response_headers, body, received


def assert_payload_too_large(status: int, headers: dict[str, str], body: bytes) -> None:
    assert status == 413
    assert headers["content-type"].startswith("application/json")
    assert json.loads(body)["error"]["code"] == "payload_too_large"
    assert headers["content-security-policy"].startswith(EXPECTED_CSP_START)
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert int(headers["content-length"]) == len(body)


def split(data: bytes) -> list[bytes]:
    return [data[i : i + CHUNK] for i in range(0, len(data), CHUNK)]


@pytest.mark.parametrize(
    ("path", "method"),
    [("/api/v1/auth/login", "POST"), ("/api/v1/health", "GET"), ("/khong-ton-tai", "POST")],
    ids=["api-post", "api-get", "unknown-route"],
)
async def test_declared_content_length_over_limit_returns_json_413_without_reading_body(
    limited_app: FastAPI, path: str, method: str
) -> None:
    body = b"x" * (BODY_LIMIT * 3)

    status, headers, response, received = await send_raw(
        limited_app,
        path,
        split(body),
        [(b"content-length", str(len(body)).encode()), (b"content-type", b"application/json")],
        method=method,
    )

    assert_payload_too_large(status, headers, response)
    assert received == 0  # bị chặn trước khi đọc bất kỳ byte nào


async def test_streamed_body_without_content_length_returns_json_413(limited_app: FastAPI) -> None:
    body = b"x" * (BODY_LIMIT * 3)

    status, headers, response, received = await send_raw(
        limited_app, "/api/v1/auth/login", split(body), [(b"content-type", b"application/json")]
    )

    assert_payload_too_large(status, headers, response)
    assert received < len(split(body))  # dừng đọc ngay sau khi vượt


async def test_understated_content_length_is_not_trusted(limited_app: FastAPI) -> None:
    body = b"x" * (BODY_LIMIT * 3)

    status, headers, response, _ = await send_raw(
        limited_app,
        "/api/v1/auth/login",
        split(body),
        [(b"content-length", b"10"), (b"content-type", b"application/json")],
    )

    assert_payload_too_large(status, headers, response)


async def test_multipart_upload_over_body_limit_returns_json_413(limited_app: FastAPI) -> None:
    # Đã đăng nhập + CSRF: chỉ khi đó endpoint mới đọc body (request ẩn danh bị 401 trước khi đọc,
    # xem test_media_upload_order.py) → kiểm tra đúng nhánh vượt giới hạn GIỮA CHỪNG khi đọc body
    await create_schema(limited_app)
    transport = ASGITransport(app=limited_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await register_and_login(client)
        cookie = "; ".join(f"{name}={value}" for name, value in client.cookies.items())
        csrf_token = csrf_headers(client)[CSRF_HEADER]
    boundary = b"limit-boundary"
    body = (
        b"--" + boundary + b'\r\nContent-Disposition: form-data; name="file"; filename="a.png"\r\n'
        b"Content-Type: image/png\r\n\r\n"
        + b"x" * (BODY_LIMIT * 3)
        + b"\r\n--"
        + boundary
        + b"--\r\n"
    )

    status, headers, response, _ = await send_raw(
        limited_app,
        "/api/v1/media/images",
        split(body),
        [
            (b"content-type", b"multipart/form-data; boundary=" + boundary),
            (b"cookie", cookie.encode()),
            (CSRF_HEADER.lower().encode(), csrf_token.encode()),
        ],
    )

    assert_payload_too_large(status, headers, response)


async def test_body_within_limit_is_unaffected(limited_app: FastAPI) -> None:
    transport = ASGITransport(app=limited_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/auth/login", json={"email": "bad"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_other_error_responses_are_unchanged(limited_app: FastAPI) -> None:
    transport = ASGITransport(app=limited_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        not_found = await client.get("/api/v1/khong-ton-tai")
        not_allowed = await client.delete("/api/v1/health")
        unauthorized = await client.get("/api/v1/auth/me")

    assert (not_found.status_code, not_found.json()["error"]["code"]) == (404, "not_found")
    assert (not_allowed.status_code, not_allowed.json()["error"]["code"]) == (
        405,
        "method_not_allowed",
    )
    assert (unauthorized.status_code, unauthorized.json()["error"]["code"]) == (401, "unauthorized")


def test_default_body_limit_covers_image_limit_plus_multipart_overhead() -> None:
    settings = build_settings()

    assert settings.image_upload_max_bytes == 10_485_760
    assert settings.max_request_body_bytes == 11 * 1024 * 1024
    assert settings.max_request_body_bytes > settings.image_upload_max_bytes


def test_body_limit_must_exceed_image_limit() -> None:
    with pytest.raises(ValidationError, match="max_request_body_bytes"):
        build_settings(image_upload_max_bytes=1024, max_request_body_bytes=1024)
