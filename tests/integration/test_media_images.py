"""API ảnh đầu vào (P2-4): upload, metadata, nội dung; quyền sở hữu, hạn mức, hoàn tác, file tạm."""

import hashlib
import io
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import starlette.formparsers as formparsers
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from PIL import Image
from sqlalchemy import func, select

from app.core.config import Settings
from app.main import create_app
from app.modules.media.errors import ImageProcessingBusyError
from app.modules.media.image_processing import ImageFormat
from app.modules.media.models import MediaAsset
from app.modules.media.service import MediaService
from app.modules.quota.service import StorageQuotaService
from tests.conftest import build_settings, create_schema, sqlite_file_url
from tests.helpers import csrf_headers, register_and_login
from tests.image_samples import (
    FORBIDDEN_METADATA,
    ICC_PROFILE,
    make_animated,
    make_image,
    make_png_header_only,
    metadata_sections,
)

pytestmark = pytest.mark.integration

UPLOAD_URL = "/api/v1/media/images"
PUBLIC_FIELDS = {"id", "content_type", "size_bytes", "width", "height", "created_at"}
CONTENT_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
EXTENSIONS = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}


def storage_dir(settings: Settings) -> Path:
    return settings.storage_root_path


def stored_files(settings: Settings, kind: str = "uploads") -> list[Path]:
    directory = storage_dir(settings) / kind
    return sorted(directory.iterdir()) if directory.exists() else []


def temp_files(settings: Settings) -> list[Path]:
    return stored_files(settings, "tmp")


async def upload(
    client: AsyncClient,
    data: bytes,
    *,
    filename: str = "photo.png",
    content_type: str = "image/png",
    with_csrf: bool = True,
) -> Response:
    headers = csrf_headers(client) if with_csrf else {}
    return await client.post(
        UPLOAD_URL, files={"file": (filename, data, content_type)}, headers=headers
    )


async def current_user_id(client: AsyncClient) -> int:
    response = await client.get("/api/v1/auth/me")
    user_id: int = response.json()["id"]
    return user_id


async def used_bytes(app: FastAPI, user_id: int) -> int:
    settings: Settings = app.state.settings
    async with app.state.session_factory() as session:
        return await StorageQuotaService(session, settings.storage_quota_bytes_per_user).usage(
            user_id
        )


async def asset_count(app: FastAPI) -> int:
    async with app.state.session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(MediaAsset))
    return count or 0


@pytest.fixture
async def logged_in(client: AsyncClient) -> AsyncClient:
    await register_and_login(client)
    return client


async def make_client(app: FastAPI) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    )


@pytest.fixture
async def custom_app(tmp_path: Path) -> AsyncIterator[Any]:
    """Tạo app với cấu hình riêng (giới hạn nhỏ) trên DB + storage tạm."""
    created: list[FastAPI] = []

    async def factory(**overrides: Any) -> FastAPI:
        settings = build_settings(
            database_url=sqlite_file_url(tmp_path),
            storage_root=str(tmp_path / "storage"),
            **overrides,
        )
        application = create_app(settings)
        await create_schema(application)
        created.append(application)
        return application

    yield factory
    for application in created:
        application.state.image_gate.close()
        await application.state.engine.dispose()


# --------------------------------------------------------------------------- thành công
@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
async def test_upload_stores_clean_image_and_counts_real_bytes(
    app: FastAPI, logged_in: AsyncClient, settings: Settings, fmt: str
) -> None:
    response = await upload(logged_in, make_image(fmt, (60, 40)), content_type=CONTENT_TYPES[fmt])

    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == PUBLIC_FIELDS  # không lộ storage_key, sha256, đường dẫn
    assert response.headers["location"] == f"{UPLOAD_URL}/{body['id']}"
    assert body["content_type"] == CONTENT_TYPES[fmt]
    assert (body["width"], body["height"]) == (40, 60)  # đã áp Orientation=6
    [stored] = stored_files(settings)
    assert stored.suffix == EXTENSIONS[fmt]
    assert body["size_bytes"] == stored.stat().st_size
    data = stored.read_bytes()
    assert set(metadata_sections(data, fmt)).isdisjoint(FORBIDDEN_METADATA[fmt])
    with Image.open(io.BytesIO(data)) as saved:
        assert dict(saved.getexif()) == {}
        assert saved.info.get("icc_profile") == ICC_PROFILE
    assert await used_bytes(app, await current_user_id(logged_in)) == body["size_bytes"]
    assert temp_files(settings) == []


async def test_upload_records_sha256_of_stored_file(
    app: FastAPI, logged_in: AsyncClient, settings: Settings
) -> None:
    response = await upload(logged_in, make_image("PNG"))

    [stored] = stored_files(settings)
    async with app.state.session_factory() as session:
        asset = await session.get(MediaAsset, response.json()["id"])
    assert asset is not None
    assert asset.sha256 == hashlib.sha256(stored.read_bytes()).hexdigest()
    assert asset.storage_key == f"uploads/{stored.name}"


async def test_client_content_type_and_filename_are_ignored(
    logged_in: AsyncClient, settings: Settings
) -> None:
    response = await upload(
        logged_in, make_image("PNG"), filename="x.exe", content_type="image/jpeg"
    )

    assert response.status_code == 201
    assert response.json()["content_type"] == "image/png"
    [stored] = stored_files(settings)
    assert stored.suffix == ".png"
    # tên do server sinh (32 hex), không dùng tên file của client
    assert len(stored.stem) == 32 and all(c in "0123456789abcdef" for c in stored.stem)


# --------------------------------------------------------------------------- từ chối
# Pillow chỉ cảnh báo (không chặn) khi ảnh vượt MAX_IMAGE_PIXELS (dưới 2 lần);
# kiểm tra riêng của ta vẫn từ chối
@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
@pytest.mark.parametrize(
    ("data", "content_type", "status", "code"),
    [
        (b"<!doctype html><script>alert(1)</script>", "image/png", 415, "unsupported_media_type"),
        (
            b"GIF89a\x01\x00\x01\x00\x80\x00\x00" + b"\x00" * 30,
            "image/gif",
            415,
            "unsupported_media_type",
        ),
        (b"", "image/png", 415, "unsupported_media_type"),
        (make_animated("WEBP"), "image/webp", 415, "animated_image_not_supported"),
        (make_animated("PNG"), "image/png", 415, "animated_image_not_supported"),
        (b"\x89PNG\r\n\x1a\n" + b"garbage" * 50, "image/png", 422, "invalid_image"),
        (make_png_header_only(5_000, 5_000), "image/png", 422, "image_dimensions_too_large"),
        (make_png_header_only(60_000, 60_000), "image/png", 422, "image_dimensions_too_large"),
    ],
    ids=["html", "gif", "empty", "animated-webp", "animated-png", "corrupt", "25MP", "bomb"],
)
async def test_rejected_uploads_leave_nothing_behind(
    app: FastAPI,
    logged_in: AsyncClient,
    settings: Settings,
    data: bytes,
    content_type: str,
    status: int,
    code: str,
) -> None:
    response = await upload(logged_in, data, content_type=content_type)

    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    assert stored_files(settings) == []
    assert temp_files(settings) == []
    assert await asset_count(app) == 0
    assert await used_bytes(app, await current_user_id(logged_in)) == 0


async def test_truncated_jpeg_is_rejected(logged_in: AsyncClient, settings: Settings) -> None:
    complete = make_image("JPEG", (400, 300), with_metadata=False)

    response = await upload(logged_in, complete[: len(complete) // 2], content_type="image/jpeg")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_image"
    assert temp_files(settings) == []


async def test_byte_limit_boundary(custom_app: Any) -> None:
    sample = make_image("PNG", with_metadata=False)
    exact_app = await custom_app(image_upload_max_bytes=len(sample), max_request_body_bytes=8192)
    async with await make_client(exact_app) as client:
        await register_and_login(client)
        accepted = await upload(client, sample)
        rejected = await upload(client, sample + b"\x00")

    assert accepted.status_code == 201
    assert rejected.status_code == 413
    assert rejected.json()["error"]["code"] == "payload_too_large"
    assert rejected.headers["content-security-policy"].startswith("default-src 'self'")
    assert temp_files(exact_app.state.settings) == []


# --------------------------------------------------------------------------- hạn mức
async def test_quota_exceeded_returns_409_and_cleans_up(custom_app: Any) -> None:
    small_app = await custom_app(storage_quota_bytes_per_user=100)
    async with await make_client(small_app) as client:
        await register_and_login(client)
        response = await upload(client, make_image("PNG", (80, 80), with_metadata=False))
        user_id = await current_user_id(client)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "storage_quota_exceeded"
    assert stored_files(small_app.state.settings) == []
    assert temp_files(small_app.state.settings) == []
    assert await used_bytes(small_app, user_id) == 0


async def test_quota_counts_every_upload(
    app: FastAPI, logged_in: AsyncClient, settings: Settings
) -> None:
    first = await upload(logged_in, make_image("PNG"))
    second = await upload(logged_in, make_image("JPEG"), content_type="image/jpeg")

    total = first.json()["size_bytes"] + second.json()["size_bytes"]
    assert await used_bytes(app, await current_user_id(logged_in)) == total
    assert sum(path.stat().st_size for path in stored_files(settings)) == total


# --------------------------------------------------------------------------- hoàn tác khi lỗi
async def test_commit_failure_releases_quota_and_removes_temp_files(
    app: FastAPI, logged_in: AsyncClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_replace(*_: object) -> None:
        raise PermissionError("file bị khóa (giả lập)")

    monkeypatch.setattr(os, "replace", failing_replace)
    response = await upload(logged_in, make_image("PNG"))
    monkeypatch.undo()

    assert response.status_code == 500
    assert "file bị khóa" not in response.text  # không lộ chi tiết nội bộ
    assert await used_bytes(app, await current_user_id(logged_in)) == 0
    assert stored_files(settings) == [] and temp_files(settings) == []
    assert await asset_count(app) == 0


async def test_database_failure_deletes_file_and_releases_quota(
    app: FastAPI, logged_in: AsyncClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing_insert(*_: object, **__: object) -> None:
        raise RuntimeError("DB lỗi (giả lập)")

    monkeypatch.setattr(MediaService, "_insert_asset", failing_insert)
    response = await upload(logged_in, make_image("PNG"))

    assert response.status_code == 500
    assert await used_bytes(app, await current_user_id(logged_in)) == 0
    assert stored_files(settings) == [] and temp_files(settings) == []


async def test_failed_delete_after_database_failure_keeps_quota_for_reconciliation(
    app: FastAPI,
    logged_in: AsyncClient,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def failing_insert(*_: object, **__: object) -> None:
        raise RuntimeError("DB lỗi (giả lập)")

    real_unlink = os.unlink

    def failing_unlink(path: Any, *args: Any, **kwargs: Any) -> None:
        if f"{os.sep}uploads{os.sep}" in os.fspath(path):
            raise PermissionError("file đang bị khóa (giả lập)")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(MediaService, "_insert_asset", failing_insert)
    monkeypatch.setattr(os, "unlink", failing_unlink)
    response = await upload(logged_in, make_image("PNG"))
    monkeypatch.undo()

    [orphan] = stored_files(settings)
    assert response.status_code == 500
    assert await used_bytes(app, await current_user_id(logged_in)) == orphan.stat().st_size
    assert "đối soát" in caplog.text
    assert temp_files(settings) == []


async def test_busy_gate_returns_503_with_retry_after_and_cleans_up(
    app: FastAPI, logged_in: AsyncClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def busy(*_: object, **__: object) -> None:
        raise ImageProcessingBusyError

    monkeypatch.setattr(app.state.image_gate, "run", busy)
    response = await upload(logged_in, make_image("PNG"))

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "image_processing_busy"
    assert int(response.headers["retry-after"]) > 0
    assert temp_files(settings) == []
    assert await used_bytes(app, await current_user_id(logged_in)) == 0


# --------------------------------------------------------------------------- file tạm của Starlette
def noise_png(size: int = 700) -> bytes:
    # Nhiễu ngẫu nhiên: PNG không nén được → file đủ lớn để Starlette ghi xuống đĩa
    image = Image.frombytes("RGB", (size, size), os.urandom(size * size * 3))
    out = io.BytesIO()
    image.save(out, "PNG", compress_level=0)
    return out.getvalue()


@pytest.mark.parametrize("valid", [True, False], ids=["success", "rejected"])
async def test_starlette_spooled_upload_is_closed_and_removed(
    logged_in: AsyncClient, monkeypatch: pytest.MonkeyPatch, valid: bool
) -> None:
    spools: list[Any] = []
    real_spool = formparsers.SpooledTemporaryFile  # type: ignore[attr-defined]

    def recording_spool(*args: Any, **kwargs: Any) -> Any:
        spool = real_spool(*args, **kwargs)
        spools.append(spool)
        return spool

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", recording_spool)
    data = noise_png() if valid else b"<html>" + b"x" * (1_200_000)
    assert len(data) > 1024 * 1024  # vượt ngưỡng 1 MB → Starlette ghi xuống đĩa

    response = await upload(logged_in, data)

    assert response.status_code == (201 if valid else 415)
    assert spools, "Starlette phải tạo file tạm cho upload"
    assert all(spool.closed for spool in spools)
    disk_paths = spooled_disk_paths(spools)
    if sys.platform == "win32":  # TemporaryFile trên Windows có tên file thật để kiểm tra
        assert disk_paths, "upload > 1 MB phải được ghi xuống đĩa"
    assert existing_paths(disk_paths) == []


def spooled_disk_paths(spools: list[Any]) -> list[str]:
    names = [getattr(getattr(spool, "_file", None), "name", None) for spool in spools]
    return [name for name in names if isinstance(name, str)]


def existing_paths(paths: list[str]) -> list[str]:
    return [path for path in paths if os.path.exists(path)]


# ------------------------------------------------------------------------- đọc metadata / nội dung
async def test_owner_can_read_metadata_and_content(
    logged_in: AsyncClient, settings: Settings
) -> None:
    created = (await upload(logged_in, make_image("WEBP"), content_type="image/webp")).json()

    metadata = await logged_in.get(f"{UPLOAD_URL}/{created['id']}")
    content = await logged_in.get(f"{UPLOAD_URL}/{created['id']}/content")

    assert metadata.status_code == 200
    assert metadata.json() == created
    assert content.status_code == 200
    assert content.headers["content-type"] == "image/webp"
    assert content.headers["x-content-type-options"] == "nosniff"
    assert content.headers["cache-control"] == "private, no-store"
    assert content.headers["cross-origin-resource-policy"] == "same-origin"
    [stored] = stored_files(settings)
    assert content.content == stored.read_bytes()


async def test_other_users_images_are_not_found(app: FastAPI, logged_in: AsyncClient) -> None:
    created = (await upload(logged_in, make_image("PNG"))).json()
    async with await make_client(app) as other:
        await register_and_login(other, email="bob@example.com")
        metadata = await other.get(f"{UPLOAD_URL}/{created['id']}")
        content = await other.get(f"{UPLOAD_URL}/{created['id']}/content")

    for response in (metadata, content):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("path", ["999999", "999999/content"], ids=["metadata", "content"])
async def test_missing_image_is_not_found(logged_in: AsyncClient, path: str) -> None:
    response = await logged_in.get(f"{UPLOAD_URL}/{path}")

    assert response.status_code == 404


@pytest.mark.parametrize("image_id", ["0", "-1", "abc"])
async def test_invalid_image_id_is_rejected(logged_in: AsyncClient, image_id: str) -> None:
    response = await logged_in.get(f"{UPLOAD_URL}/{image_id}")

    assert response.status_code == 422


# --------------------------------------------------------------------------- auth / CSRF
async def test_upload_requires_login(client: AsyncClient, settings: Settings) -> None:
    response = await upload(client, make_image("PNG"), with_csrf=False)

    assert response.status_code == 401
    assert stored_files(settings) == [] and temp_files(settings) == []


async def test_reading_requires_login(client: AsyncClient) -> None:
    assert (await client.get(f"{UPLOAD_URL}/1")).status_code == 401
    assert (await client.get(f"{UPLOAD_URL}/1/content")).status_code == 401


async def test_upload_requires_csrf_token(logged_in: AsyncClient, settings: Settings) -> None:
    response = await upload(logged_in, make_image("PNG"), with_csrf=False)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"
    assert stored_files(settings) == []


# --------------------------------------------------------------------------- rate limit
TINY_PNG = make_image("PNG", (4, 4), with_metadata=False)


async def test_per_user_upload_limit_is_30_per_hour(logged_in: AsyncClient) -> None:
    statuses = [(await upload(logged_in, TINY_PNG)).status_code for _ in range(30)]
    blocked = await upload(logged_in, TINY_PNG)

    assert statuses == [201] * 30
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "rate_limit_exceeded"
    assert int(blocked.headers["retry-after"]) > 0


async def test_per_ip_upload_limit_is_60_per_hour_across_users(app: FastAPI) -> None:
    clients = [await make_client(app) for _ in range(3)]
    try:
        for index, client in enumerate(clients):
            await register_and_login(client, email=f"ip-user-{index}@example.com")
        statuses = [
            (await upload(client, TINY_PNG)).status_code for client in clients for _ in range(20)
        ]
        blocked = await upload(clients[0], TINY_PNG)  # user này mới dùng 20/30 lượt riêng
    finally:
        for client in clients:
            await client.aclose()

    assert statuses == [201] * 60
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) > 0


# -------------------------------------------------------------- không truy cập file trực tiếp
async def test_stored_files_are_not_reachable_through_static_or_raw_paths(
    logged_in: AsyncClient, settings: Settings
) -> None:
    await upload(logged_in, make_image("PNG"))
    [stored] = stored_files(settings)

    for path in [
        f"/static/../storage/uploads/{stored.name}",
        f"/static/%2e%2e/storage/uploads/{stored.name}",
        f"/storage/uploads/{stored.name}",
        f"/uploads/{stored.name}",
    ]:
        response = await logged_in.get(path)
        assert response.status_code == 404, path
        assert response.content != stored.read_bytes()


async def test_missing_file_on_disk_is_not_found(
    logged_in: AsyncClient, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    created = (await upload(logged_in, make_image("PNG"))).json()
    [stored] = stored_files(settings)
    stored.unlink()

    response = await logged_in.get(f"{UPLOAD_URL}/{created['id']}/content")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert str(settings.storage_root_path) not in caplog.text  # log không lộ đường dẫn tuyệt đối


async def test_tampered_storage_key_is_never_served(app: FastAPI, logged_in: AsyncClient) -> None:
    created = (await upload(logged_in, make_image("PNG"))).json()
    async with app.state.session_factory() as session:
        asset = await session.get(MediaAsset, created["id"])
        assert asset is not None
        asset.storage_key = "../../pyproject.toml"
        await session.commit()

    response = await logged_in.get(f"{UPLOAD_URL}/{created['id']}/content")

    assert response.status_code == 404
    assert b"[project]" not in response.content


async def test_uploads_directory_linked_outside_storage_is_not_served(
    logged_in: AsyncClient, settings: Settings, tmp_path: Path
) -> None:
    created = (await upload(logged_in, make_image("PNG"))).json()
    [stored] = stored_files(settings)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / stored.name).write_bytes(b"du-lieu-ngoai-storage")
    uploads = storage_dir(settings) / "uploads"
    for item in uploads.iterdir():
        item.unlink()
    uploads.rmdir()
    if not _make_directory_link(uploads, outside):
        pytest.skip("không tạo được liên kết thư mục trên máy này")
    try:
        response = await logged_in.get(f"{UPLOAD_URL}/{created['id']}/content")
    finally:
        _remove_directory_link(uploads)

    assert response.status_code == 404
    assert b"du-lieu-ngoai-storage" not in response.content


def _make_directory_link(link: Path, target: Path) -> bool:
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except OSError:
        pass
    if sys.platform == "win32":
        import _winapi  # chỉ có trên Windows

        _winapi.CreateJunction(str(target), str(link))
        return True
    return False


def _remove_directory_link(link: Path) -> None:
    # rmdir/unlink chỉ gỡ chính liên kết, không đụng nội dung thư mục đích
    if link.is_junction():
        os.rmdir(link)
    elif link.is_symlink():
        link.unlink()


def test_image_formats_cover_whitelist() -> None:
    assert {fmt.value for fmt in ImageFormat} == set(CONTENT_TYPES)
