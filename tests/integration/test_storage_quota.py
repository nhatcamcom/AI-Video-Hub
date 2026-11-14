"""Hạn mức lưu trữ theo user (P2-3) trên file SQLite thật, kể cả nhiều request đồng thời."""

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.storage import DeleteOutcome, LocalStorage, StorageKind
from app.modules.quota.errors import QuotaUserNotFoundError, StorageQuotaExceededError
from app.modules.quota.models import UserStorageUsage
from app.modules.quota.service import StorageQuotaService
from app.modules.users.models import User
from app.modules.users.service import UserService

pytestmark = pytest.mark.integration

MIB = 1024 * 1024
ONE_GIBIBYTE = 1_073_741_824
SMALL_QUOTA = 1_000


@pytest.fixture
def session_factory(app: FastAPI) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = app.state.session_factory
    return factory


async def create_user(factory: async_sessionmaker[AsyncSession], email: str) -> int:
    async with factory() as session:
        user = await UserService(session).create(
            email=email, password_hash="not-a-real-hash-for-tests", full_name="Quota Test"
        )
        return user.id


async def usage_of(factory: async_sessionmaker[AsyncSession], user_id: int, quota: int) -> int:
    async with factory() as session:
        return await StorageQuotaService(session, quota).usage(user_id)


async def reserve_in_own_session(
    factory: async_sessionmaker[AsyncSession], user_id: int, size_bytes: int, quota: int
) -> bool:
    async with factory() as session:
        try:
            await StorageQuotaService(session, quota).reserve(user_id, size_bytes)
        except StorageQuotaExceededError:
            return False
        return True


async def release_in_own_session(
    factory: async_sessionmaker[AsyncSession], user_id: int, size_bytes: int, quota: int
) -> bool:
    async with factory() as session:
        return await StorageQuotaService(session, quota).release(user_id, size_bytes)


@pytest.fixture
async def user_id(session_factory: async_sessionmaker[AsyncSession]) -> int:
    return await create_user(session_factory, "quota-owner@example.com")


# -------------------- giữ chỗ
async def test_usage_is_zero_before_any_reservation(
    session_factory: async_sessionmaker[AsyncSession], user_id: int
) -> None:
    assert await usage_of(session_factory, user_id, SMALL_QUOTA) == 0


async def test_reserve_creates_row_lazily_and_adds_bytes(
    session_factory: async_sessionmaker[AsyncSession], user_id: int
) -> None:
    assert await reserve_in_own_session(session_factory, user_id, 300, SMALL_QUOTA)
    assert await reserve_in_own_session(session_factory, user_id, 200, SMALL_QUOTA)

    assert await usage_of(session_factory, user_id, SMALL_QUOTA) == 500


async def test_reserve_up_to_exact_quota_succeeds_and_one_more_byte_fails(
    session_factory: async_sessionmaker[AsyncSession], user_id: int
) -> None:
    assert await reserve_in_own_session(session_factory, user_id, SMALL_QUOTA - 1, SMALL_QUOTA)
    assert await reserve_in_own_session(session_factory, user_id, 1, SMALL_QUOTA)

    assert not await reserve_in_own_session(session_factory, user_id, 1, SMALL_QUOTA)
    assert await usage_of(session_factory, user_id, SMALL_QUOTA) == SMALL_QUOTA


async def test_single_reservation_larger_than_quota_is_rejected_without_row(
    session_factory: async_sessionmaker[AsyncSession], user_id: int
) -> None:
    assert not await reserve_in_own_session(session_factory, user_id, SMALL_QUOTA + 1, SMALL_QUOTA)

    async with session_factory() as session:
        rows = await session.scalar(select(func.count()).select_from(UserStorageUsage))
    assert rows == 0


async def test_one_gibibyte_quota_boundary(
    session_factory: async_sessionmaker[AsyncSession], user_id: int
) -> None:
    assert await reserve_in_own_session(session_factory, user_id, ONE_GIBIBYTE, ONE_GIBIBYTE)
    assert not await reserve_in_own_session(session_factory, user_id, 1, ONE_GIBIBYTE)


async def test_quota_error_maps_to_409_storage_quota_exceeded() -> None:
    error = StorageQuotaExceededError()

    assert error.status_code == 409
    assert error.code == "storage_quota_exceeded"


@pytest.mark.parametrize("size_bytes", [0, -1, True], ids=["zero", "negative", "bool"])
async def test_reserve_rejects_non_positive_sizes(
    session_factory: async_sessionmaker[AsyncSession], user_id: int, size_bytes: int
) -> None:
    async with session_factory() as session:
        with pytest.raises(ValueError, match="size_bytes"):
            await StorageQuotaService(session, SMALL_QUOTA).reserve(user_id, size_bytes)


async def test_reserve_for_unknown_user_raises(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        with pytest.raises(QuotaUserNotFoundError):
            await StorageQuotaService(session, SMALL_QUOTA).reserve(999_999, 10)


async def test_users_have_independent_quotas(
    session_factory: async_sessionmaker[AsyncSession], user_id: int
) -> None:
    other_id = await create_user(session_factory, "quota-other@example.com")

    assert await reserve_in_own_session(session_factory, user_id, SMALL_QUOTA, SMALL_QUOTA)
    assert await reserve_in_own_session(session_factory, other_id, SMALL_QUOTA, SMALL_QUOTA)
    assert not await reserve_in_own_session(session_factory, user_id, 1, SMALL_QUOTA)

    assert await usage_of(session_factory, other_id, SMALL_QUOTA) == SMALL_QUOTA


async def test_reserve_and_release_leave_no_open_transaction(
    session_factory: async_sessionmaker[AsyncSession], user_id: int
) -> None:
    async with session_factory() as session:
        service = StorageQuotaService(session, SMALL_QUOTA)

        await service.reserve(user_id, 10)
        assert not session.in_transaction()
        with pytest.raises(StorageQuotaExceededError):
            await service.reserve(user_id, SMALL_QUOTA)
        assert not session.in_transaction()
        await service.release(user_id, 10)
        assert not session.in_transaction()


# -------------------- đồng thời (SQLite file thật)
@pytest.mark.parametrize("round_number", [1, 2, 3])
async def test_concurrent_reservations_never_exceed_one_gibibyte(
    session_factory: async_sessionmaker[AsyncSession], user_id: int, round_number: int
) -> None:
    attempts = 20
    size = 100 * MIB  # 10 x 100 MiB = 1000 MiB vừa đủ; lần thứ 11 sẽ vượt 1024 MiB

    results = await asyncio.gather(
        *(
            reserve_in_own_session(session_factory, user_id, size, ONE_GIBIBYTE)
            for _ in range(attempts)
        )
    )

    used = await usage_of(session_factory, user_id, ONE_GIBIBYTE)
    assert sum(results) == ONE_GIBIBYTE // size == 10
    assert used == 10 * size
    assert used <= ONE_GIBIBYTE


async def test_concurrent_first_reservations_create_exactly_one_row(
    session_factory: async_sessionmaker[AsyncSession], user_id: int
) -> None:
    results = await asyncio.gather(
        *(reserve_in_own_session(session_factory, user_id, 1, SMALL_QUOTA) for _ in range(8))
    )

    async with session_factory() as session:
        rows = await session.scalar(select(func.count()).select_from(UserStorageUsage))
    assert all(results)
    assert rows == 1
    assert await usage_of(session_factory, user_id, SMALL_QUOTA) == 8


async def test_concurrent_releases_never_underflow(
    session_factory: async_sessionmaker[AsyncSession], user_id: int
) -> None:
    assert await reserve_in_own_session(session_factory, user_id, 100, SMALL_QUOTA)

    results = await asyncio.gather(
        *(release_in_own_session(session_factory, user_id, 60, SMALL_QUOTA) for _ in range(3))
    )

    assert sum(results) == 1
    assert await usage_of(session_factory, user_id, SMALL_QUOTA) == 40


# -------------------- hoàn lại (quyết định 7)
async def test_release_subtracts_exact_bytes(
    session_factory: async_sessionmaker[AsyncSession], user_id: int
) -> None:
    assert await reserve_in_own_session(session_factory, user_id, 700, SMALL_QUOTA)

    assert await release_in_own_session(session_factory, user_id, 300, SMALL_QUOTA)
    assert await usage_of(session_factory, user_id, SMALL_QUOTA) == 400


async def test_release_more_than_used_changes_nothing_and_warns(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert await reserve_in_own_session(session_factory, user_id, 100, SMALL_QUOTA)

    with caplog.at_level(logging.WARNING, logger="app.modules.quota.service"):
        released = await release_in_own_session(session_factory, user_id, 101, SMALL_QUOTA)

    assert released is False
    assert await usage_of(session_factory, user_id, SMALL_QUOTA) == 100  # KHÔNG đưa về 0
    assert "đối soát" in caplog.text
    assert "example.com" not in caplog.text  # không ghi email vào log


async def test_release_without_row_is_unsuccessful(
    session_factory: async_sessionmaker[AsyncSession], user_id: int
) -> None:
    assert await release_in_own_session(session_factory, user_id, 1, SMALL_QUOTA) is False


@pytest.mark.parametrize("size_bytes", [0, -5], ids=["zero", "negative"])
async def test_release_rejects_non_positive_sizes(
    session_factory: async_sessionmaker[AsyncSession], user_id: int, size_bytes: int
) -> None:
    async with session_factory() as session:
        with pytest.raises(ValueError, match="size_bytes"):
            await StorageQuotaService(session, SMALL_QUOTA).release(user_id, size_bytes)


# -------------------- đối soát
async def test_set_used_records_measured_total_for_reconciliation(
    session_factory: async_sessionmaker[AsyncSession], user_id: int
) -> None:
    async with session_factory() as session:
        await StorageQuotaService(session, SMALL_QUOTA).set_used(user_id, 250)

    assert await usage_of(session_factory, user_id, SMALL_QUOTA) == 250
    assert await reserve_in_own_session(session_factory, user_id, SMALL_QUOTA - 250, SMALL_QUOTA)
    assert not await reserve_in_own_session(session_factory, user_id, 1, SMALL_QUOTA)


async def test_set_used_rejects_negative_values(
    session_factory: async_sessionmaker[AsyncSession], user_id: int
) -> None:
    async with session_factory() as session:
        with pytest.raises(ValueError, match="used_bytes"):
            await StorageQuotaService(session, SMALL_QUOTA).set_used(user_id, -1)


async def test_deleting_user_cascades_usage_row(
    session_factory: async_sessionmaker[AsyncSession], user_id: int
) -> None:
    assert await reserve_in_own_session(session_factory, user_id, 10, SMALL_QUOTA)

    async with session_factory() as session:
        await session.execute(delete(User).where(User.id == user_id))
        await session.commit()
        rows = await session.scalar(select(func.count()).select_from(UserStorageUsage))

    assert rows == 0


async def test_database_check_constraint_rejects_negative_usage(
    session_factory: async_sessionmaker[AsyncSession], user_id: int
) -> None:
    from sqlalchemy.exc import IntegrityError

    async with session_factory() as session:
        session.add(UserStorageUsage(user_id=user_id, used_bytes=-1))
        with pytest.raises(IntegrityError):
            await session.commit()


# -------------------- kết hợp với LocalStorage
async def chunks_of(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


async def test_commit_failure_after_reservation_is_compensated(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = LocalStorage(tmp_path / "media")
    temp = await storage.write_temp_file(chunks_of(b"x" * 120))
    assert await reserve_in_own_session(session_factory, user_id, temp.size_bytes, SMALL_QUOTA)

    def failing_replace(*_: object) -> None:
        raise PermissionError("file bị khóa (giả lập)")

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(PermissionError):
        await storage.commit_temp_file(temp, storage.new_key(StorageKind.UPLOADS, ".png"))
    monkeypatch.undo()
    assert await release_in_own_session(session_factory, user_id, temp.size_bytes, SMALL_QUOTA)
    await storage.discard_temp_file(temp)

    assert await usage_of(session_factory, user_id, SMALL_QUOTA) == 0
    assert list((tmp_path / "media" / "tmp").iterdir()) == []


async def test_failed_delete_keeps_quota_and_successful_delete_releases_it(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = LocalStorage(tmp_path / "media")
    temp = await storage.write_temp_file(chunks_of(b"v" * 64))
    assert await reserve_in_own_session(session_factory, user_id, temp.size_bytes, SMALL_QUOTA)
    key = storage.new_key(StorageKind.VIDEOS, ".mp4")
    await storage.commit_temp_file(temp, key)

    def failing_unlink(*_: object) -> None:
        raise PermissionError("file đang được đọc (giả lập)")

    monkeypatch.setattr(os, "unlink", failing_unlink)
    assert await storage.delete(key) is DeleteOutcome.FAILED
    monkeypatch.undo()
    assert await usage_of(session_factory, user_id, SMALL_QUOTA) == 64  # 7.7: không giải phóng

    assert await storage.delete(key) is DeleteOutcome.DELETED
    assert await release_in_own_session(session_factory, user_id, 64, SMALL_QUOTA)
    assert await usage_of(session_factory, user_id, SMALL_QUOTA) == 0
