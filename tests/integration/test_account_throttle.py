"""Bộ đếm theo tài khoản trong database (GCRA): đúng hạn mức, hồi phục, dọn bảng, nguyên tử.

Chạy trên SQLite thật (file) — không mock tầng database.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.database import Base, create_engine, create_session_factory
from app.core.rate_limit import RateLimitRule
from app.modules.auth.models import AuthThrottle
from app.modules.auth.throttle import PURGE_BATCH_SIZE, AccountThrottle
from tests.conftest import build_settings, sqlite_file_url

pytestmark = pytest.mark.integration

RULE = RateLimitRule(max_requests=3, window_seconds=60, name="test:rule")
DAILY = RateLimitRule(max_requests=5, window_seconds=86_400, name="test:daily")
KEY = b"k" * 32
START = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    database = create_engine(build_settings(database_url=sqlite_file_url(tmp_path)))
    async with database.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield database
    await database.dispose()


@pytest.fixture
def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


async def reserve(
    sessions: async_sessionmaker[AsyncSession],
    subject: str,
    now: datetime,
    rules: tuple[RateLimitRule, ...] = (RULE,),
) -> int | None:
    async with sessions() as session:
        return await AccountThrottle(session, KEY).reserve(rules, subject, now)


async def row_count(sessions: async_sessionmaker[AsyncSession]) -> int:
    async with sessions() as session:
        return (await session.execute(select(func.count()).select_from(AuthThrottle))).scalar_one()


async def test_allows_a_burst_of_max_requests_then_blocks(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    results = [await reserve(sessions, "a@example.com", START) for _ in range(4)]

    assert results[:3] == [None, None, None]
    assert results[3] == 20  # một lượt hồi sau window / max = 20 giây


async def test_budget_recovers_one_request_per_interval(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    for _ in range(3):
        await reserve(sessions, "a@example.com", START)

    assert await reserve(sessions, "a@example.com", START + timedelta(seconds=19)) == 1
    assert await reserve(sessions, "a@example.com", START + timedelta(seconds=20)) is None
    assert await reserve(sessions, "a@example.com", START + timedelta(seconds=20)) is not None
    # Sau trọn một cửa sổ không có lượt nào → đầy lại cả burst
    later = START + timedelta(seconds=200)
    assert [await reserve(sessions, "a@example.com", later) for _ in range(3)] == [None] * 3


async def test_subjects_and_rules_are_independent(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    for _ in range(3):
        await reserve(sessions, "a@example.com", START)

    assert await reserve(sessions, "b@example.com", START) is None
    assert await reserve(sessions, "a@example.com", START, rules=(DAILY,)) is None


async def test_longest_rule_blocks_when_short_rule_still_has_room(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    rules = (RULE, DAILY)
    now = START
    allowed = 0
    for _ in range(20):  # cách nhau đủ xa để giới hạn ngắn luôn hồi, giới hạn ngày thì không
        allowed += await reserve(sessions, "a@example.com", now, rules) is None
        now += timedelta(seconds=60)

    assert allowed == DAILY.max_requests


async def test_clear_restores_the_full_budget(sessions: async_sessionmaker[AsyncSession]) -> None:
    for _ in range(3):
        await reserve(sessions, "a@example.com", START)

    async with sessions() as session:
        await AccountThrottle(session, KEY).clear((RULE,), "a@example.com")
        await session.commit()

    assert await reserve(sessions, "a@example.com", START) is None


async def test_keys_are_hmac_not_raw_email(sessions: async_sessionmaker[AsyncSession]) -> None:
    await reserve(sessions, "secret.person@example.com", START)

    async with sessions() as session:
        stored = (await session.execute(select(AuthThrottle.key_hash))).scalar_one()
        other_key = AccountThrottle(session, b"x" * 32).key_for(RULE, "secret.person@example.com")

    assert "secret" not in stored
    assert len(stored) == 64
    assert stored != other_key  # khóa khác → không đoán được hàng của email nào


async def test_recovered_rows_are_purged_as_new_rows_arrive(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    for index in range(PURGE_BATCH_SIZE):  # 100 email giả, mỗi email một lượt
        await reserve(sessions, f"fake{index}@example.net", START)
    assert await row_count(sessions) == PURGE_BATCH_SIZE

    await reserve(sessions, "new@example.net", START + timedelta(seconds=60))

    assert await row_count(sessions) == 1  # chỉ còn hàng còn hiệu lực


async def test_purge_recovered_removes_only_recovered_rows(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await reserve(sessions, "old@example.com", START)  # hồi đầy lúc START + 20 giây
    for _ in range(3):
        await reserve(sessions, "busy@example.com", START)  # hồi đầy lúc START + 60 giây

    async with sessions() as session:
        deleted = await AccountThrottle(session, KEY).purge_recovered(START + timedelta(seconds=25))
        await session.commit()

    assert deleted == 1
    assert await row_count(sessions) == 1


async def test_concurrent_reservations_never_exceed_the_budget(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    results = await asyncio.gather(
        *(reserve(sessions, "hot@example.com", START) for _ in range(30))
    )

    assert sum(result is None for result in results) == RULE.max_requests
    assert await row_count(sessions) == 1  # chèn đồng thời cùng khóa không tạo hàng trùng
