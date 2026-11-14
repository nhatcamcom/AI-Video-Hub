"""Thứ tự xử lý upload ảnh: đăng nhập, CSRF, rate limit IP rồi user đều được kiểm tra TRƯỚC khi
đọc body multipart; hợp đồng lỗi 422 / OpenAPI giữ nguyên như khi endpoint khai báo tham số File."""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import starlette.formparsers as formparsers
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.types import Message

from app.core.config import Settings
from app.main import create_app
from tests.conftest import build_settings, create_schema, sqlite_file_url
from tests.helpers import CSRF_HEADER, csrf_headers, register_and_login
from tests.image_samples import make_image

pytestmark = pytest.mark.integration

UPLOAD_URL = "/api/v1/media/images"
BOUNDARY = "order-test-boundary"
LARGE_FILE_BYTES = 3 * 1024 * 1024  # > 1 MB: nếu bị parse, Starlette sẽ ghi file tạm xuống đĩa
CHUNK_BYTES = 64 * 1024
TINY_PNG = make_image("PNG", (4, 4), with_metadata=False)
CLIENT_IP = "127.0.0.1"  # trùng địa chỉ client của ASGITransport → chung bộ đếm rate limit IP
# Response 422 chụp từ endpoint TRƯỚC khi chuyển việc parse form vào thân hàm (hợp đồng API cũ)
MISSING_FILE_RESPONSE = {
    "error": {
        "code": "validation_error",
        "message": "Dữ liệu không hợp lệ",
        "details": [{"field": "body.file", "message": "Field required", "code": "missing"}],
    }
}


def multipart_body(parts: list[tuple[str, bytes, str | None]]) -> bytes:
    """parts: (tên field, dữ liệu, tên file hoặc None nếu là field văn bản)."""
    chunks = []
    for name, data, filename in parts:
        disposition = f'form-data; name="{name}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        header = f"--{BOUNDARY}\r\nContent-Disposition: {disposition}\r\n"
        if filename is not None:
            header += "Content-Type: image/png\r\n"
        chunks.append(header.encode() + b"\r\n" + data + b"\r\n")
    return b"".join(chunks) + f"--{BOUNDARY}--\r\n".encode()


LARGE_UPLOAD = multipart_body([("file", b"\x89PNG" + b"\x00" * LARGE_FILE_BYTES, "big.png")])


async def send_counting(
    app: FastAPI,
    body: bytes,
    headers: list[tuple[bytes, bytes]],
    *,
    declare_length: bool = True,
) -> tuple[int, dict[str, Any], int]:
    """POST ASGI thô; trả (status, body JSON, số byte body mà app đã thực sự kéo về)."""
    position = 0
    body_bytes_read = 0
    messages: list[Message] = []
    finished = asyncio.Event()

    async def receive() -> Message:
        nonlocal position, body_bytes_read
        if position >= len(body):
            await finished.wait()
            return {"type": "http.disconnect"}
        chunk = body[position : position + CHUNK_BYTES]
        position += len(chunk)
        body_bytes_read += len(chunk)
        return {"type": "http.request", "body": chunk, "more_body": position < len(body)}

    async def send(message: Message) -> None:
        messages.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body"):
            finished.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": UPLOAD_URL,
        "raw_path": UPLOAD_URL.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"host", b"test"),
            (b"content-type", f"multipart/form-data; boundary={BOUNDARY}".encode()),
            *([(b"content-length", str(len(body)).encode())] if declare_length else []),
            *headers,
        ],
        "client": (CLIENT_IP, 50000),
        "server": ("test", 80),
    }
    await app(scope, receive, send)
    start = next(m for m in messages if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return start["status"], json.loads(payload), body_bytes_read


def session_headers(client: AsyncClient, *, with_csrf: bool = True) -> list[tuple[bytes, bytes]]:
    cookie = "; ".join(f"{name}={value}" for name, value in client.cookies.items())
    headers = [(b"cookie", cookie.encode())]
    if with_csrf:
        headers.append((CSRF_HEADER.lower().encode(), csrf_headers(client)[CSRF_HEADER].encode()))
    return headers


@pytest.fixture
def spools(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Ghi lại mọi file tạm mà Starlette tạo khi parse multipart."""
    created: list[Any] = []
    real_spool = formparsers.SpooledTemporaryFile  # type: ignore[attr-defined]

    def recording_spool(*args: Any, **kwargs: Any) -> Any:
        spool = real_spool(*args, **kwargs)
        created.append(spool)
        return spool

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", recording_spool)
    return created


def assert_nothing_written(settings: Settings, spools: list[Any]) -> None:
    assert spools == []  # Starlette không tạo file tạm nào
    temp_dir = settings.storage_root_path / "tmp"
    assert not temp_dir.exists() or list(temp_dir.iterdir()) == []


async def make_client(app: FastAPI) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    )


# --------------------------------------------------------------------------- từ chối trước body
async def test_anonymous_upload_is_rejected_before_reading_body(
    app: FastAPI, settings: Settings, spools: list[Any]
) -> None:
    status, payload, body_bytes_read = await send_counting(app, LARGE_UPLOAD, headers=[])

    assert status == 401
    assert payload["error"]["code"] == "unauthorized"
    assert body_bytes_read == 0
    assert_nothing_written(settings, spools)


async def test_upload_without_csrf_is_rejected_before_reading_body(
    app: FastAPI, client: AsyncClient, settings: Settings, spools: list[Any]
) -> None:
    await register_and_login(client)

    status, payload, body_bytes_read = await send_counting(
        app, LARGE_UPLOAD, headers=session_headers(client, with_csrf=False)
    )

    assert status == 403
    assert payload["error"]["code"] == "csrf_failed"
    assert body_bytes_read == 0
    assert_nothing_written(settings, spools)


async def test_ip_rate_limited_upload_is_rejected_before_reading_body(
    app: FastAPI, settings: Settings, spools: list[Any]
) -> None:
    clients = [await make_client(app) for _ in range(3)]
    try:
        for index, client in enumerate(clients):
            await register_and_login(client, email=f"order-ip-{index}@example.com")
        for client in clients:
            for _ in range(20):  # 3 x 20 = 60 lượt/IP; mỗi user mới dùng 20/30 lượt riêng
                assert (await upload_tiny(client)).status_code == 201
        spools.clear()

        status, payload, body_bytes_read = await send_counting(
            app, LARGE_UPLOAD, headers=session_headers(clients[0])
        )
    finally:
        for client in clients:
            await client.aclose()

    assert status == 429
    assert payload["error"]["code"] == "rate_limit_exceeded"
    assert body_bytes_read == 0
    assert_nothing_written(settings, spools)


async def test_user_rate_limited_upload_is_rejected_before_reading_body(
    app: FastAPI, client: AsyncClient, settings: Settings, spools: list[Any]
) -> None:
    await register_and_login(client)
    for _ in range(30):
        assert (await upload_tiny(client)).status_code == 201
    spools.clear()

    status, payload, body_bytes_read = await send_counting(
        app, LARGE_UPLOAD, headers=session_headers(client)
    )

    assert status == 429
    assert payload["error"]["code"] == "rate_limit_exceeded"
    assert body_bytes_read == 0
    assert_nothing_written(settings, spools)


async def test_accepted_upload_reads_the_whole_body(app: FastAPI, client: AsyncClient) -> None:
    await register_and_login(client)
    body = multipart_body([("file", TINY_PNG, "tiny.png")])

    status, _, body_bytes_read = await send_counting(app, body, headers=session_headers(client))

    assert status == 201
    assert body_bytes_read == len(body)


async def test_body_over_limit_while_parsing_closes_and_removes_spooled_files(
    tmp_path: Path, spools: list[Any]
) -> None:
    application = create_app(
        build_settings(
            database_url=sqlite_file_url(tmp_path),
            storage_root=str(tmp_path / "storage"),
            image_upload_max_bytes=2 * 1024 * 1024,
            max_request_body_bytes=2 * 1024 * 1024 + 512 * 1024,
        )
    )
    await create_schema(application)
    try:
        async with await make_client(application) as client:
            await register_and_login(client)
            headers = session_headers(client)
        # Không khai Content-Length → vượt giới hạn GIỮA CHỪNG, sau khi Starlette đã ghi file tạm
        status, payload, _ = await send_counting(
            application, LARGE_UPLOAD, headers=headers, declare_length=False
        )
    finally:
        application.state.image_gate.close()
        await application.state.engine.dispose()

    assert status == 413
    assert payload["error"]["code"] == "payload_too_large"
    assert spools, "phải đã bắt đầu parse multipart"
    assert all(spool.closed for spool in spools)
    assert existing_disk_files(spools) == []


def existing_disk_files(spools: list[Any]) -> list[str]:
    names = [getattr(getattr(spool, "_file", None), "name", None) for spool in spools]
    return [name for name in names if isinstance(name, str) and Path(name).exists()]


async def upload_tiny(client: AsyncClient) -> Any:
    return await client.post(
        UPLOAD_URL,
        files={"file": ("tiny.png", TINY_PNG, "image/png")},
        headers=csrf_headers(client),
    )


# --------------------------------------------------------------------------- hợp đồng API
@pytest.fixture
async def logged_in(client: AsyncClient) -> AsyncClient:
    await register_and_login(client)
    return client


async def test_missing_file_field_keeps_previous_422_response(logged_in: AsyncClient) -> None:
    response = await logged_in.post(
        UPLOAD_URL,
        files={"other": ("a.png", TINY_PNG, "image/png")},
        headers=csrf_headers(logged_in),
    )

    assert response.status_code == 422
    assert response.json() == MISSING_FILE_RESPONSE


async def test_empty_multipart_keeps_previous_422_response(logged_in: AsyncClient) -> None:
    response = await logged_in.post(
        UPLOAD_URL,
        content=f"--{BOUNDARY}--\r\n".encode(),
        headers={
            **csrf_headers(logged_in),
            "Content-Type": f"multipart/form-data; boundary={BOUNDARY}",
        },
    )

    assert response.status_code == 422
    assert response.json() == MISSING_FILE_RESPONSE


@pytest.mark.parametrize(
    "request_options",
    [{"json": {"file": "abc"}}, {}],
    ids=["json-body", "no-body"],
)
async def test_non_multipart_body_keeps_previous_422_response(
    logged_in: AsyncClient, request_options: dict[str, Any]
) -> None:
    response = await logged_in.post(UPLOAD_URL, headers=csrf_headers(logged_in), **request_options)

    assert response.status_code == 422
    assert response.json() == MISSING_FILE_RESPONSE


async def test_two_files_are_rejected(logged_in: AsyncClient, settings: Settings) -> None:
    response = await logged_in.post(
        UPLOAD_URL,
        files=[
            ("file", ("a.png", TINY_PNG, "image/png")),
            ("file", ("b.png", TINY_PNG, "image/png")),
        ],
        headers=csrf_headers(logged_in),
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "http_error"
    assert not (settings.storage_root_path / "uploads").exists()


async def test_extra_text_field_is_rejected(logged_in: AsyncClient, settings: Settings) -> None:
    response = await logged_in.post(
        UPLOAD_URL,
        files={"file": ("a.png", TINY_PNG, "image/png")},
        data={"title": "không được gửi field thừa"},
        headers=csrf_headers(logged_in),
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "http_error"
    assert not (settings.storage_root_path / "uploads").exists()


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


async def test_openapi_describes_required_multipart_file(development_app: FastAPI) -> None:
    async with await make_client(development_app) as client:
        spec = (await client.get("/openapi.json")).json()

    request_body = spec["paths"][UPLOAD_URL]["post"]["requestBody"]
    assert request_body["required"] is True
    assert list(request_body["content"]) == ["multipart/form-data"]
    schema = request_body["content"]["multipart/form-data"]["schema"]
    if "$ref" in schema:
        schema = spec["components"]["schemas"][schema["$ref"].rsplit("/", 1)[-1]]
    assert schema["type"] == "object"
    assert schema["required"] == ["file"]
    assert schema["properties"]["file"]["type"] == "string"
    assert schema["properties"]["file"]["contentMediaType"] == "application/octet-stream"
