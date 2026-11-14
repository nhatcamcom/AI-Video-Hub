"""Registry task nền dùng chung (W8) và dừng an toàn (W10/W11).

Task giả "cố ý trì hoãn kết thúc" luôn được giải phóng trong `finally` và được chờ bằng
`asyncio.wait_for(..., timeout)` → test không thể treo.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest

from app.core.background_tasks import (
    BackgroundTaskRegistry,
    ShutdownResult,
    shutdown_background,
)

pytestmark = pytest.mark.unit

GUARD_SECONDS = 5  # timeout bảo vệ: không test nào được chờ lâu hơn
SHORT = 0.05


class FakeEngine:
    def __init__(self) -> None:
        self.disposed = 0

    async def dispose(self) -> None:
        self.disposed += 1


@pytest.fixture
async def registry() -> AsyncIterator[BackgroundTaskRegistry]:
    tracked = BackgroundTaskRegistry()
    yield tracked
    leftovers = tracked.pending()
    for task in leftovers:
        task.cancel()
    if leftovers:
        await asyncio.wait_for(asyncio.gather(*leftovers, return_exceptions=True), GUARD_SECONDS)
    assert tracked.pending() == ()


async def waiting(event: asyncio.Event) -> None:
    await event.wait()


async def stubborn(release: asyncio.Event, cancelled: list[int]) -> None:
    """Cố ý trì hoãn: bị hủy vẫn chờ tới khi test giải phóng rồi MỚI ném lại CancelledError."""
    try:
        await release.wait()
    except asyncio.CancelledError:
        cancelled.append(1)
        await asyncio.wait_for(release.wait(), GUARD_SECONDS)  # có giới hạn, không nuốt vô hạn
        raise


async def test_spawned_task_is_tracked_until_done(registry: BackgroundTaskRegistry) -> None:
    done = asyncio.Event()
    task = registry.spawn(waiting(done), name="demo")

    assert registry.pending() == (task,)
    done.set()
    await asyncio.wait_for(task, GUARD_SECONDS)
    await asyncio.sleep(0)  # cho done_callback chạy

    assert registry.pending() == ()
    assert task.get_name() == "demo"


async def test_track_keeps_a_strong_reference(registry: BackgroundTaskRegistry) -> None:
    done = asyncio.Event()
    registry.track(asyncio.create_task(waiting(done)))  # phía gọi không giữ biến nào

    assert len(registry.pending()) == 1
    done.set()
    await registry.drain(GUARD_SECONDS)


async def test_drain_returns_empty_when_all_tasks_finish(registry: BackgroundTaskRegistry) -> None:
    done = asyncio.Event()
    registry.spawn(waiting(done), name="a")
    asyncio.get_running_loop().call_later(SHORT, done.set)

    assert await registry.drain(GUARD_SECONDS) == ()


async def test_drain_follows_tasks_spawned_while_draining(registry: BackgroundTaskRegistry) -> None:
    second_done = asyncio.Event()

    async def spawns_another() -> None:
        registry.spawn(waiting(second_done), name="child")

    registry.spawn(spawns_another(), name="parent")
    asyncio.get_running_loop().call_later(SHORT * 2, second_done.set)

    assert await registry.drain(GUARD_SECONDS) == ()  # chờ cả task con phát sinh trong lúc drain


async def test_drain_timeout_returns_live_tasks_and_keeps_references(
    registry: BackgroundTaskRegistry,
) -> None:
    release = asyncio.Event()
    task = registry.spawn(waiting(release), name="slow")
    try:
        alive = await registry.drain(SHORT)

        assert alive == (task,)
        assert registry.pending() == (task,)  # drain không bỏ tham chiếu
        assert not task.cancelled()  # drain không tự hủy task
    finally:
        release.set()
        await asyncio.wait_for(task, GUARD_SECONDS)


async def test_shutdown_disposes_engine_only_after_drain(registry: BackgroundTaskRegistry) -> None:
    engine = FakeEngine()
    done = asyncio.Event()
    task = registry.spawn(waiting(done), name="cleanup")
    asyncio.get_running_loop().call_later(SHORT, done.set)

    result = await shutdown_background(
        registry, engine, drain_seconds=GUARD_SECONDS, cancel_grace_seconds=1
    )

    assert result == ShutdownResult(engine_disposed=True, alive=0)
    assert task.done() and engine.disposed == 1


async def test_shutdown_cancels_overdue_tasks_then_disposes(
    registry: BackgroundTaskRegistry,
) -> None:
    engine = FakeEngine()
    task = registry.spawn(waiting(asyncio.Event()), name="overdue")

    result = await shutdown_background(
        registry, engine, drain_seconds=SHORT, cancel_grace_seconds=1
    )

    assert result == ShutdownResult(engine_disposed=True, alive=0)
    assert task.cancelled() and engine.disposed == 1


async def test_shutdown_never_disposes_while_a_task_is_still_alive(
    registry: BackgroundTaskRegistry, caplog: pytest.LogCaptureFixture
) -> None:
    engine = FakeEngine()
    release = asyncio.Event()
    cancelled: list[int] = []
    task = registry.spawn(stubborn(release, cancelled), name="stubborn-cleanup")
    try:
        with caplog.at_level(logging.CRITICAL):
            result = await shutdown_background(
                registry, engine, drain_seconds=SHORT, cancel_grace_seconds=SHORT
            )

        assert result == ShutdownResult(engine_disposed=False, alive=1)
        assert engine.disposed == 0  # task còn có thể dùng session → KHÔNG dispose
        assert cancelled == [1]
        assert registry.pending() == (task,)  # vẫn giữ tham chiếu
        assert "stubborn-cleanup" in caplog.text and "cần đối soát" in caplog.text
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), GUARD_SECONDS)
    assert task.cancelled()  # CancelledError cuối cùng vẫn được ném lại, không bị nuốt
    assert registry.pending() == ()


async def test_shutdown_with_no_tasks_disposes_immediately(
    registry: BackgroundTaskRegistry,
) -> None:
    engine = FakeEngine()

    result = await shutdown_background(registry, engine, drain_seconds=0, cancel_grace_seconds=0)

    assert result == ShutdownResult(engine_disposed=True, alive=0)


async def test_task_exception_is_logged_not_lost(
    registry: BackgroundTaskRegistry, caplog: pytest.LogCaptureFixture
) -> None:
    async def failing() -> Any:
        raise RuntimeError("lỗi giả lập")

    task = registry.spawn(failing(), name="broken")
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), GUARD_SECONDS)
    await asyncio.sleep(0)

    assert "broken" in caplog.text and "RuntimeError" in caplog.text
    assert registry.pending() == ()
