"""Tranh chấp slot khi tạo video đồng thời trên file SQLite thật (P2-6).

Chỉ ACTIVE_SLOT_CONFLICT được thử lại; lỗi FK / CHECK / lỗi lạ không bao giờ thành 409.
"""

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import delete, func, select

from app.modules.media.models import MediaAsset
from app.modules.users.models import User
from app.modules.videos import service as video_service_module
from app.modules.videos.models import Video
from app.modules.videos.service import MAX_SLOT_ATTEMPTS, VideoService
from tests.helpers import csrf_headers, register_and_login

pytestmark = pytest.mark.integration

URL = "/api/v1/videos"
PROMPT = "prompt-bi-mat-khong-duoc-ghi-log"
VALID: dict[str, Any] = {
    "prompt": PROMPT,
    "aspect_ratio": "16:9",
    "duration_seconds": 5,
    "model_key": "mock",
}
RACE_TIMEOUT_SECONDS = 10


async def create(client: AsyncClient, **overrides: Any) -> Response:
    return await client.post(URL, json={**VALID, **overrides}, headers=csrf_headers(client))


async def user_id(client: AsyncClient) -> int:
    value: int = (await client.get("/api/v1/auth/me")).json()["id"]
    return value


async def active_slots(app: FastAPI) -> list[int | None]:
    async with app.state.session_factory() as session:
        return sorted(
            (await session.scalars(select(Video.active_slot))).all(), key=lambda s: s or 0
        )


async def video_count(app: FastAPI) -> int:
    async with app.state.session_factory() as session:
        return (await session.scalar(select(func.count()).select_from(Video))) or 0


@pytest.fixture
async def logged_in(client: AsyncClient) -> AsyncClient:
    await register_and_login(client)
    return client


def count_slot_reads(monkeypatch: pytest.MonkeyPatch) -> list[int | None]:
    reads: list[int | None] = []
    original = VideoService._first_free_slot

    async def recording(self: VideoService, owner_id: int) -> int | None:
        slot = await original(self, owner_id)
        reads.append(slot)
        return slot

    monkeypatch.setattr(VideoService, "_first_free_slot", recording)
    return reads


def make_racers_read_together(monkeypatch: pytest.MonkeyPatch, racers: int = 2) -> None:
    """`racers` request đầu tiên cùng đọc slot trống RỒI mới chèn → chắc chắn tranh chấp."""
    original = VideoService._first_free_slot
    barrier = asyncio.Barrier(racers)
    calls = 0

    async def racing(self: VideoService, owner_id: int) -> int | None:
        nonlocal calls
        calls += 1
        slot = await original(self, owner_id)
        if calls <= racers:
            await asyncio.wait_for(barrier.wait(), RACE_TIMEOUT_SECONDS)
        return slot

    monkeypatch.setattr(VideoService, "_first_free_slot", racing)


# --------------------------------------------------------------------------- tranh chấp slot
async def test_two_requests_racing_for_slot_1_both_succeed_after_one_retry(
    app: FastAPI, logged_in: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_racers_read_together(monkeypatch)
    reads = count_slot_reads(monkeypatch)

    responses = await asyncio.gather(create(logged_in), create(logged_in))

    assert [response.status_code for response in responses] == [202, 202]
    assert await active_slots(app) == [1, 2]
    assert sorted(reads, key=lambda s: s or 0) == [1, 1, 2]  # đúng một lần thử lại, lấy slot 2


async def test_retry_finds_no_free_slot_and_returns_409(
    app: FastAPI, logged_in: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (await create(logged_in)).status_code == 202  # slot 1 đã bị chiếm
    make_racers_read_together(monkeypatch)

    responses = await asyncio.gather(create(logged_in), create(logged_in))

    assert sorted(response.status_code for response in responses) == [202, 409]
    conflict = next(response for response in responses if response.status_code == 409)
    assert conflict.json()["error"]["code"] == "active_video_limit_reached"
    assert await active_slots(app) == [1, 2]


async def test_persistent_slot_conflict_with_free_slot_reported_is_500(
    app: FastAPI,
    logged_in: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert (await create(logged_in)).status_code == 202  # slot 1 đã bị chiếm
    attempts = 0

    async def always_slot_one(self: VideoService, owner_id: int) -> int:
        nonlocal attempts
        attempts += 1
        return 1

    monkeypatch.setattr(VideoService, "_first_free_slot", always_slot_one)
    response = await create(logged_in)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert attempts == MAX_SLOT_ATTEMPTS == 3
    assert await video_count(app) == 1
    assert PROMPT not in caplog.text


async def test_ten_concurrent_requests_create_exactly_two_jobs(
    app: FastAPI, logged_in: AsyncClient
) -> None:
    responses = await asyncio.gather(*(create(logged_in) for _ in range(10)))

    statuses = sorted(response.status_code for response in responses)
    assert statuses == [202, 202] + [409] * 8
    assert await active_slots(app) == [1, 2]


async def test_concurrent_requests_of_two_users_are_independent(app: FastAPI) -> None:
    clients = [
        AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
        )
        for _ in range(2)
    ]
    try:
        for index, client in enumerate(clients):
            await register_and_login(client, email=f"race-{index}@example.com")
        responses = await asyncio.gather(*(create(client) for client in clients for _ in range(4)))
    finally:
        for client in clients:
            await client.aclose()

    assert sorted(response.status_code for response in responses) == [202] * 4 + [409] * 4
    assert await active_slots(app) == [1, 1, 2, 2]


# ----------------------------------------------------------------- lỗi KHÁC tranh chấp slot
def run_before_insert(monkeypatch: pytest.MonkeyPatch, action: Any) -> None:
    original = VideoService._insert_video

    async def hooked(self: VideoService, *args: Any, **kwargs: Any) -> Video:
        await action()
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(VideoService, "_insert_video", hooked)


async def test_image_deleted_before_insert_is_422_not_409(
    app: FastAPI, logged_in: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = await user_id(logged_in)
    async with app.state.session_factory() as session:
        image = MediaAsset(
            owner_id=owner,
            storage_key="uploads/" + "f" * 32 + ".png",
            content_type="image/png",
            size_bytes=10,
            width=4,
            height=4,
            sha256="0" * 64,
        )
        session.add(image)
        await session.commit()
        image_id = image.id

    async def hard_delete_image() -> None:
        async with app.state.session_factory() as session:
            await session.execute(delete(MediaAsset).where(MediaAsset.id == image_id))
            await session.commit()

    reads = count_slot_reads(monkeypatch)
    run_before_insert(monkeypatch, hard_delete_image)
    response = await create(logged_in, input_image_id=image_id)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "input_image_not_found"
    assert len(reads) == 1  # lỗi FK KHÔNG được thử lại như tranh chấp slot
    assert await video_count(app) == 0


async def test_owner_deleted_before_insert_is_500_not_409(
    app: FastAPI,
    logged_in: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    owner = await user_id(logged_in)

    async def delete_owner() -> None:
        async with app.state.session_factory() as session:
            await session.execute(delete(User).where(User.id == owner))
            await session.commit()

    reads = count_slot_reads(monkeypatch)
    run_before_insert(monkeypatch, delete_owner)
    response = await create(logged_in)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert len(reads) == 1
    assert await video_count(app) == 0
    assert PROMPT not in caplog.text


async def test_check_violation_is_500_not_409(
    app: FastAPI, logged_in: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(video_service_module, "ACTIVE_SLOTS", (3,))  # slot ngoài CHECK (1, 2)
    reads = count_slot_reads(monkeypatch)

    response = await create(logged_in)

    assert response.status_code == 500
    assert len(reads) == 1
    assert await video_count(app) == 0


async def test_slot_is_free_after_rolled_back_integrity_error(
    app: FastAPI, logged_in: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(video_service_module, "ACTIVE_SLOTS", (3,))
    assert (await create(logged_in)).status_code == 500
    monkeypatch.undo()

    responses = [await create(logged_in), await create(logged_in)]

    assert [response.status_code for response in responses] == [202, 202]
