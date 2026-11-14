"""POST /api/v1/videos và GET /api/v1/videos/{id} (P2-6)."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import func, select, update

from app.core.time import utc_now
from app.main import create_app
from app.modules.auth.models import AuthThrottle
from app.modules.auth.throttle import AccountThrottle
from app.modules.media.models import MediaAsset
from app.modules.quota.service import StorageQuotaService
from app.modules.videos.catalog import (
    MOCK_VIDEO_MODEL,
    AspectRatio,
    VideoModelSpec,
    build_video_model_catalog,
)
from app.modules.videos.models import Video, VideoStatus
from app.modules.videos.rate_limits import VIDEO_CREATE_PER_USER, video_user_subject
from app.modules.videos.service import VideoService
from tests.conftest import build_settings, create_schema, sqlite_file_url
from tests.helpers import csrf_headers, register_and_login
from tests.image_samples import make_image

pytestmark = pytest.mark.integration

URL = "/api/v1/videos"
PROMPT = "Một chú mèo đi dạo trên bãi biển lúc hoàng hôn"
VALID: dict[str, Any] = {
    "prompt": PROMPT,
    "aspect_ratio": "16:9",
    "duration_seconds": 5,
    "model_key": "mock",
}
RESPONSE_FIELDS = {
    "id",
    "status",
    "progress",
    "prompt",
    "aspect_ratio",
    "duration_seconds",
    "model_key",
    "input_image_id",
    "error_code",
    "created_at",
    "started_at",
    "finished_at",
}
LANDSCAPE_ONLY = VideoModelSpec(
    key="fake-landscape",
    display_name="Fake landscape",
    is_demo=True,
    aspect_ratios=(AspectRatio.LANDSCAPE,),
    durations_seconds=(5,),
    supports_input_image=False,
)
DISABLED = VideoModelSpec(
    key="fake-disabled",
    display_name="Fake disabled",
    is_demo=True,
    aspect_ratios=(AspectRatio.LANDSCAPE,),
    durations_seconds=(5,),
    supports_input_image=True,
)


async def create(client: AsyncClient, **overrides: Any) -> Response:
    return await client.post(URL, json={**VALID, **overrides}, headers=csrf_headers(client))


async def user_id(client: AsyncClient) -> int:
    value: int = (await client.get("/api/v1/auth/me")).json()["id"]
    return value


async def video_count(app: FastAPI) -> int:
    async with app.state.session_factory() as session:
        return (await session.scalar(select(func.count()).select_from(Video))) or 0


async def add_image(app: FastAPI, owner_id: int, *, deleted: bool = False) -> int:
    async with app.state.session_factory() as session:
        image = MediaAsset(
            owner_id=owner_id,
            storage_key=f"uploads/{owner_id:032x}.png",
            content_type="image/png",
            size_bytes=10,
            width=4,
            height=4,
            sha256="0" * 64,
            deleted_at=utc_now() if deleted else None,
        )
        session.add(image)
        await session.commit()
        return image.id


async def finish_video(app: FastAPI, video_id: int) -> None:
    async with app.state.session_factory() as session:
        await session.execute(
            update(Video)
            .where(Video.id == video_id)
            .values(status=VideoStatus.FAILED, active_slot=None)
        )
        await session.commit()


async def new_client(app: FastAPI) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    )


@pytest.fixture
async def logged_in(client: AsyncClient) -> AsyncClient:
    await register_and_login(client)
    return client


@pytest.fixture
async def other(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with await new_client(app) as client:
        await register_and_login(client, email="bob@example.com")
        yield client


# --------------------------------------------------------------------------- tạo thành công
async def test_create_returns_202_with_location_and_queued_job(
    app: FastAPI, logged_in: AsyncClient
) -> None:
    response = await create(logged_in)

    assert response.status_code == 202, response.text
    body = response.json()
    assert set(body) == RESPONSE_FIELDS  # không có storage key, kích thước file, slot nội bộ
    assert response.headers["location"] == f"{URL}/{body['id']}"
    assert body["status"] == "queued"
    assert body["progress"] == 0
    assert body["prompt"] == PROMPT
    assert body["model_key"] == "mock"
    assert body["input_image_id"] is None
    assert body["error_code"] is None
    assert body["started_at"] is None and body["finished_at"] is None
    async with app.state.session_factory() as session:
        row = await session.get(Video, body["id"])
    assert row is not None
    assert (row.owner_id, row.status, row.active_slot) == (await user_id(logged_in), "queued", 1)


async def test_detail_matches_created_video(logged_in: AsyncClient) -> None:
    created = (await create(logged_in)).json()

    detail = await logged_in.get(f"{URL}/{created['id']}")

    assert detail.status_code == 200
    assert detail.json() == created


async def test_create_with_own_uploaded_image(app: FastAPI, logged_in: AsyncClient) -> None:
    uploaded = await logged_in.post(
        "/api/v1/media/images",
        files={"file": ("a.png", make_image("PNG"), "image/png")},
        headers=csrf_headers(logged_in),
    )

    response = await create(logged_in, input_image_id=uploaded.json()["id"])

    assert response.status_code == 202
    assert response.json()["input_image_id"] == uploaded.json()["id"]


async def test_prompt_is_stored_stripped(logged_in: AsyncClient) -> None:
    response = await create(logged_in, prompt=f"   {PROMPT}  \n")

    assert response.json()["prompt"] == PROMPT


@pytest.mark.parametrize(("ratio", "duration"), [("9:16", 10), ("1:1", 5)])
async def test_every_mock_option_is_accepted(
    logged_in: AsyncClient, ratio: str, duration: int
) -> None:
    response = await create(logged_in, aspect_ratio=ratio, duration_seconds=duration)

    assert response.status_code == 202
    assert (response.json()["aspect_ratio"], response.json()["duration_seconds"]) == (
        ratio,
        duration,
    )


# --------------------------------------------------------------------------- xác thực / CSRF
async def test_anonymous_request_is_unauthorized_even_with_invalid_body(
    app: FastAPI, client: AsyncClient
) -> None:
    valid = await client.post(URL, json=VALID)
    invalid = await client.post(URL, json={"prompt": ""})

    assert valid.status_code == invalid.status_code == 401
    assert await video_count(app) == 0


async def test_missing_csrf_is_forbidden(app: FastAPI, logged_in: AsyncClient) -> None:
    response = await logged_in.post(URL, json=VALID)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"
    assert await video_count(app) == 0


# --------------------------------------------------------------------------- model / tùy chọn
async def test_invalid_body_is_validation_error(logged_in: AsyncClient) -> None:
    response = await create(logged_in, prompt="a" * 1001)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize("model_key", ["sora", "fake-disabled"], ids=["unknown", "disabled"])
async def test_unknown_or_disabled_model_is_unavailable(
    app: FastAPI, logged_in: AsyncClient, model_key: str
) -> None:
    app.state.video_model_catalog = build_video_model_catalog(
        ["mock"], known=(MOCK_VIDEO_MODEL, DISABLED)
    )

    response = await create(logged_in, model_key=model_key)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "video_model_unavailable"
    assert await video_count(app) == 0


async def test_unknown_and_disabled_models_get_identical_responses(
    app: FastAPI, logged_in: AsyncClient
) -> None:
    app.state.video_model_catalog = build_video_model_catalog(
        ["mock"], known=(MOCK_VIDEO_MODEL, DISABLED)
    )

    unknown = await create(logged_in, model_key="sora")
    disabled = await create(logged_in, model_key="fake-disabled")

    assert unknown.json() == disabled.json()


@pytest.mark.parametrize(
    ("model_key", "ratio", "duration"),
    [("mock", "16:9", 7), ("fake-landscape", "1:1", 5), ("fake-landscape", "16:9", 10)],
    ids=["mock-duration-7", "landscape-only-square", "landscape-only-10s"],
)
async def test_options_outside_model_capabilities_are_rejected(
    app: FastAPI, logged_in: AsyncClient, model_key: str, ratio: str, duration: int
) -> None:
    app.state.video_model_catalog = build_video_model_catalog(
        ["mock", "fake-landscape"], known=(MOCK_VIDEO_MODEL, LANDSCAPE_ONLY)
    )

    response = await create(
        logged_in, model_key=model_key, aspect_ratio=ratio, duration_seconds=duration
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unsupported_video_options"
    assert await video_count(app) == 0


async def test_model_without_image_support_rejects_input_image(
    app: FastAPI, logged_in: AsyncClient
) -> None:
    app.state.video_model_catalog = build_video_model_catalog(
        ["fake-landscape"], known=(LANDSCAPE_ONLY,)
    )
    image_id = await add_image(app, await user_id(logged_in))

    response = await create(logged_in, model_key="fake-landscape", input_image_id=image_id)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "input_image_not_supported"


# --------------------------------------------------------------------------- quyền sở hữu ảnh
async def test_missing_foreign_and_deleted_images_get_identical_422(
    app: FastAPI, logged_in: AsyncClient, other: AsyncClient
) -> None:
    foreign = await add_image(app, await user_id(other))
    deleted = await add_image(app, await user_id(logged_in), deleted=True)

    responses = [
        await create(logged_in, input_image_id=image_id) for image_id in (999_999, foreign, deleted)
    ]

    assert [response.status_code for response in responses] == [422, 422, 422]
    assert responses[0].json()["error"]["code"] == "input_image_not_found"
    assert responses[0].json() == responses[1].json() == responses[2].json()
    assert await video_count(app) == 0


# --------------------------------------------------------------------------- hạn mức lưu trữ
async def set_used(app: FastAPI, owner_id: int, used_bytes: int) -> None:
    async with app.state.session_factory() as session:
        await StorageQuotaService(
            session, app.state.settings.storage_quota_bytes_per_user
        ).set_used(owner_id, used_bytes)


async def used(app: FastAPI, owner_id: int) -> int:
    async with app.state.session_factory() as session:
        return await StorageQuotaService(
            session, app.state.settings.storage_quota_bytes_per_user
        ).usage(owner_id)


async def test_full_quota_is_rejected_early(app: FastAPI, logged_in: AsyncClient) -> None:
    owner = await user_id(logged_in)
    await set_used(app, owner, app.state.settings.storage_quota_bytes_per_user)

    response = await create(logged_in)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "storage_quota_exceeded"
    assert await video_count(app) == 0


async def test_creating_a_job_does_not_reserve_quota(app: FastAPI, logged_in: AsyncClient) -> None:
    owner = await user_id(logged_in)
    almost_full = app.state.settings.storage_quota_bytes_per_user - 1
    await set_used(app, owner, almost_full)

    response = await create(logged_in)

    assert response.status_code == 202
    assert await used(app, owner) == almost_full  # P2-6 không giữ chỗ (quyết định D1)


# --------------------------------------------------------------------------- 2 job đang chạy
async def test_third_active_job_is_rejected(
    app: FastAPI, logged_in: AsyncClient, other: AsyncClient
) -> None:
    first, second = await create(logged_in), await create(logged_in)
    third = await create(logged_in)

    assert (first.status_code, second.status_code) == (202, 202)
    assert third.status_code == 409
    assert third.json()["error"]["code"] == "active_video_limit_reached"
    assert await video_count(app) == 2
    assert (await create(other)).status_code == 202  # user khác độc lập


async def test_finished_job_frees_its_slot(app: FastAPI, logged_in: AsyncClient) -> None:
    first = (await create(logged_in)).json()
    await create(logged_in)
    await finish_video(app, first["id"])

    response = await create(logged_in)

    assert response.status_code == 202
    async with app.state.session_factory() as session:
        row = await session.get(Video, response.json()["id"])
    assert row is not None and row.active_slot == 1  # dùng lại đúng slot vừa được trả


# --------------------------------------------------------------------------- đọc chi tiết
async def test_other_users_video_is_not_found(logged_in: AsyncClient, other: AsyncClient) -> None:
    created = (await create(logged_in)).json()

    response = await other.get(f"{URL}/{created['id']}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_missing_and_deleted_videos_are_not_found(
    app: FastAPI, logged_in: AsyncClient
) -> None:
    created = (await create(logged_in)).json()
    async with app.state.session_factory() as session:
        await session.execute(
            update(Video).where(Video.id == created["id"]).values(deleted_at=utc_now())
        )
        await session.commit()

    assert (await logged_in.get(f"{URL}/{created['id']}")).status_code == 404
    assert (await logged_in.get(f"{URL}/999999")).status_code == 404


@pytest.mark.parametrize("video_id", ["0", "-1", "abc"])
async def test_invalid_video_id_is_rejected(logged_in: AsyncClient, video_id: str) -> None:
    assert (await logged_in.get(f"{URL}/{video_id}")).status_code == 422


async def test_reading_requires_login(client: AsyncClient) -> None:
    assert (await client.get(f"{URL}/1")).status_code == 401


# --------------------------------------------------------------------------- rate limit
async def test_per_user_limit_is_30_per_hour_and_counts_rejected_requests(
    logged_in: AsyncClient,
) -> None:
    statuses = [(await create(logged_in)).status_code for _ in range(30)]
    blocked = await create(logged_in)

    assert statuses == [202, 202] + [409] * 28  # lượt bị từ chối vẫn bị tính
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "rate_limit_exceeded"
    assert int(blocked.headers["retry-after"]) > 0


async def test_per_ip_limit_is_60_per_hour_across_users(app: FastAPI) -> None:
    clients = [await new_client(app) for _ in range(3)]
    try:
        for index, client in enumerate(clients):
            await register_and_login(client, email=f"video-ip-{index}@example.com")
        statuses = [(await create(client)).status_code for client in clients for _ in range(20)]
        blocked = await create(clients[0])  # user này mới dùng 20/30 lượt riêng
    finally:
        for client in clients:
            await client.aclose()

    assert statuses.count(429) == 0 and len(statuses) == 60
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) > 0


# --------------------------------------------------------------------------- hoàn tác
async def test_insert_failure_rolls_back_and_keeps_counted_attempt(
    app: FastAPI,
    logged_in: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    owner = await user_id(logged_in)

    async def failing_insert(*_: object, **__: object) -> None:
        raise RuntimeError("DB lỗi (giả lập)")

    monkeypatch.setattr(VideoService, "_insert_video", failing_insert)
    response = await create(logged_in)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "DB lỗi" not in response.text
    assert await video_count(app) == 0
    assert await used(app, owner) == 0
    async with app.state.session_factory() as session:
        key = AccountThrottle(session, app.state.settings.throttle_key).key_for(
            VIDEO_CREATE_PER_USER, video_user_subject(owner)
        )
        assert await session.get(AuthThrottle, key) is not None  # lượt rate limit vẫn được tính
    assert PROMPT not in caplog.text  # không ghi prompt vào log


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


async def test_openapi_documents_video_endpoints(development_app: FastAPI) -> None:
    async with await new_client(development_app) as client:
        spec = (await client.get("/openapi.json")).json()

    create_operation = spec["paths"][URL]["post"]
    assert create_operation["responses"]["202"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/VideoResponse"
    }
    assert {"401", "403", "409", "422", "429"} <= set(create_operation["responses"])
    assert create_operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/VideoCreate"
    }
    detail_operation = spec["paths"][f"{URL}/{{video_id}}"]["get"]
    assert {"200", "401", "404"} <= set(detail_operation["responses"])
    assert set(spec["components"]["schemas"]["VideoResponse"]["properties"]) == RESPONSE_FIELDS
