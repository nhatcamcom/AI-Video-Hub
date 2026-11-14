"""Ràng buộc DB của bảng videos (P2-6): database tự bảo đảm tối đa 2 job đang chạy mỗi user,
trạng thái/slot nhất quán và giá trị hợp lệ, kể cả khi bỏ qua tầng service."""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_now
from app.modules.media.models import MediaAsset
from app.modules.users.models import User
from app.modules.videos.models import (
    ACTIVE_SLOT_CONSTRAINT_NAME,
    MAX_ACTIVE_VIDEOS_PER_USER,
    Video,
    VideoStatus,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def session(app: FastAPI) -> AsyncIterator[AsyncSession]:
    async with app.state.session_factory() as db_session:
        yield db_session


async def add_user(session: AsyncSession, email: str = "owner@example.com") -> int:
    user = User(email=email, password_hash="x", full_name="Owner")
    session.add(user)
    await session.commit()
    return user.id


async def add_image(session: AsyncSession, owner: int) -> int:
    image = MediaAsset(
        owner_id=owner,
        storage_key="uploads/" + "a" * 32 + ".png",
        content_type="image/png",
        size_bytes=10,
        width=4,
        height=4,
        sha256="0" * 64,
    )
    session.add(image)
    await session.commit()
    return image.id


def video(owner_id: int, **overrides: Any) -> Video:
    # Nhận id dạng số: sau rollback, đối tượng ORM bị expire và không được lazy-load trong async
    values: dict[str, Any] = {
        "owner_id": owner_id,
        "prompt": "biển",
        "aspect_ratio": "16:9",
        "duration_seconds": 5,
        "model_key": "mock",
        "status": VideoStatus.QUEUED,
        "active_slot": 1,
        **overrides,
    }
    if values["status"] == VideoStatus.PROCESSING and "lease_token" not in overrides:
        # P2-7: job đang xử lý bắt buộc có lease (CHECK lease_matches_status)
        values |= {"lease_token": "a" * 32, "lease_expires_at": utc_now()}
    return Video(**values)


async def insert(session: AsyncSession, row: Video) -> Video:
    session.add(row)
    await session.commit()
    return row


async def assert_rejected(session: AsyncSession, row: Video) -> None:
    session.add(row)
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


def test_constraint_names_and_limit() -> None:
    assert ACTIVE_SLOT_CONSTRAINT_NAME == "uq_videos_owner_id_active_slot"
    assert MAX_ACTIVE_VIDEOS_PER_USER == 2


async def test_new_video_defaults(session: AsyncSession) -> None:
    owner = await add_user(session)

    row = await insert(session, video(owner))

    assert row.status == VideoStatus.QUEUED
    assert row.progress == 0
    assert row.created_at is not None
    assert row.output_storage_key is None and row.deleted_at is None


async def test_database_allows_at_most_two_active_slots_per_user(session: AsyncSession) -> None:
    owner = await add_user(session)
    other = await add_user(session, "other@example.com")
    await insert(session, video(owner, active_slot=1))
    await insert(session, video(owner, active_slot=2, status=VideoStatus.PROCESSING))

    await assert_rejected(session, video(owner, active_slot=1))
    await assert_rejected(session, video(owner, active_slot=2))
    await assert_rejected(session, video(owner, active_slot=3))
    await insert(session, video(other, active_slot=1))  # user khác độc lập


async def test_finished_videos_do_not_hold_a_slot(session: AsyncSession) -> None:
    owner = await add_user(session)
    for status in (VideoStatus.SUCCEEDED, VideoStatus.FAILED, VideoStatus.FAILED):
        await insert(session, video(owner, status=status, active_slot=None))

    await insert(session, video(owner, active_slot=1))
    await insert(session, video(owner, active_slot=2))


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": VideoStatus.QUEUED, "active_slot": None},
        {"status": VideoStatus.PROCESSING, "active_slot": None},
        {"status": VideoStatus.SUCCEEDED, "active_slot": 1},
        {"status": VideoStatus.FAILED, "active_slot": 2},
        {"status": "cancelled", "active_slot": None},
        {"active_slot": 0},
        {"progress": 101},
        {"progress": -1},
        {"duration_seconds": 7},
        {"aspect_ratio": "4:3"},
        {"prompt": ""},
        {"prompt": "a" * 1001},
        {"output_storage_key": "videos/" + "b" * 32 + ".mp4"},
        {"output_size_bytes": 10},
        {"output_storage_key": "videos/" + "b" * 32 + ".mp4", "output_size_bytes": 0},
        {"poster_storage_key": "posters/" + "c" * 32 + ".png"},
        {"poster_size_bytes": 10},
    ],
    ids=[
        "queued-without-slot",
        "processing-without-slot",
        "succeeded-with-slot",
        "failed-with-slot",
        "unknown-status",
        "slot-zero",
        "progress-101",
        "progress-negative",
        "duration-7",
        "ratio-4:3",
        "empty-prompt",
        "prompt-1001",
        "output-key-without-size",
        "output-size-without-key",
        "output-size-zero",
        "poster-key-without-size",
        "poster-size-without-key",
    ],
)
async def test_invalid_rows_are_rejected(session: AsyncSession, overrides: dict[str, Any]) -> None:
    owner = await add_user(session)

    await assert_rejected(session, video(owner, **overrides))


async def test_prompt_of_1000_characters_fits(session: AsyncSession) -> None:
    owner = await add_user(session)

    row = await insert(session, video(owner, prompt="ệ" * 1000))

    assert len(row.prompt) == 1000


async def test_unknown_owner_or_image_violates_foreign_keys(session: AsyncSession) -> None:
    owner = await add_user(session)

    await assert_rejected(session, video(999_999))
    await assert_rejected(session, video(owner, input_image_id=999_999))


async def test_deleting_user_cascades_to_videos(session: AsyncSession) -> None:
    owner = await add_user(session)
    await insert(session, video(owner))

    await session.execute(delete(User).where(User.id == owner))
    await session.commit()

    assert (await session.scalars(select(Video.id))).all() == []


async def test_deleting_image_keeps_video_and_clears_reference(session: AsyncSession) -> None:
    owner = await add_user(session)
    image = await add_image(session, owner)
    video_id = (await insert(session, video(owner, input_image_id=image))).id

    await session.execute(delete(MediaAsset).where(MediaAsset.id == image))
    await session.commit()
    session.expire_all()

    reloaded = await session.get(Video, video_id)
    assert reloaded is not None and reloaded.input_image_id is None


async def test_finished_video_can_record_output(session: AsyncSession) -> None:
    owner = await add_user(session)

    row = await insert(
        session,
        video(
            owner,
            status=VideoStatus.SUCCEEDED,
            active_slot=None,
            progress=100,
            output_storage_key="videos/" + "d" * 32 + ".mp4",
            output_size_bytes=1234,
            finished_at=utc_now(),
        ),
    )

    assert row.output_size_bytes == 1234


# ------------------------------------------------------------- lease (P2-7, migration 0007)
async def test_new_video_has_no_lease_and_zero_attempts(session: AsyncSession) -> None:
    owner = await add_user(session)

    row = await insert(session, video(owner))

    assert (row.lease_token, row.lease_expires_at, row.next_attempt_at) == (None, None, None)
    assert row.attempt_count == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": VideoStatus.PROCESSING, "lease_token": None, "lease_expires_at": None},
        {"status": VideoStatus.PROCESSING, "lease_token": "a" * 32, "lease_expires_at": None},
        {"status": VideoStatus.PROCESSING, "lease_token": None, "lease_expires_at": utc_now()},
        {"status": VideoStatus.QUEUED, "lease_token": "a" * 32, "lease_expires_at": utc_now()},
        {
            "status": VideoStatus.FAILED,
            "active_slot": None,
            "lease_token": "a" * 32,
            "lease_expires_at": utc_now(),
        },
        {"attempt_count": -1},
    ],
    ids=[
        "processing-without-lease",
        "processing-without-expiry",
        "processing-without-token",
        "queued-with-lease",
        "failed-with-lease",
        "negative-attempts",
    ],
)
async def test_invalid_lease_rows_are_rejected(
    session: AsyncSession, overrides: dict[str, Any]
) -> None:
    owner = await add_user(session)

    await assert_rejected(session, video(owner, **overrides))


async def test_processing_video_with_lease_is_accepted(session: AsyncSession) -> None:
    owner = await add_user(session)

    row = await insert(
        session,
        video(
            owner,
            status=VideoStatus.PROCESSING,
            lease_token="b" * 32,
            lease_expires_at=utc_now(),
            attempt_count=1,
        ),
    )

    assert row.attempt_count == 1
