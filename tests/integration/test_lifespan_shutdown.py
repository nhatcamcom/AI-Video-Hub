"""Web app dừng an toàn (W10): lifespan chờ task dọn dẹp nền rồi mới dispose engine."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.background_tasks import BACKGROUND_TASKS
from app.main import create_app
from app.modules.media.service import pending_cleanups
from tests.conftest import build_settings, sqlite_file_url

pytestmark = pytest.mark.integration

GUARD_SECONDS = 5
SHORT = 0.05


@pytest.fixture
async def quick_app(tmp_path: Any) -> AsyncIterator[FastAPI]:
    application = create_app(
        build_settings(
            database_url=sqlite_file_url(tmp_path),
            storage_root=str(tmp_path / "storage"),
            shutdown_drain_seconds=SHORT,
            shutdown_cancel_grace_seconds=SHORT,
        )
    )
    yield application
    await application.state.engine.dispose()


def count_dispose(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    engine = app.state.engine
    real_dispose = AsyncEngine.dispose

    async def dispose(self: AsyncEngine, *args: Any, **kwargs: Any) -> None:
        if self is engine:
            calls.append(1)
        await real_dispose(self, *args, **kwargs)

    monkeypatch.setattr(AsyncEngine, "dispose", dispose)  # AsyncEngine không cho gán trên instance
    return calls


async def test_lifespan_waits_for_cleanup_before_disposing(
    quick_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    disposed = count_dispose(quick_app, monkeypatch)
    order: list[str] = []
    release = asyncio.Event()

    async def cleanup() -> None:
        await release.wait()
        order.append("cleanup")

    async with quick_app.router.lifespan_context(quick_app):
        BACKGROUND_TASKS.spawn(cleanup(), name="media-cleanup")
        asyncio.get_running_loop().call_later(SHORT / 2, release.set)

    order.append("disposed" if disposed else "not-disposed")
    assert order == ["cleanup", "disposed"]


async def test_lifespan_does_not_dispose_while_a_cleanup_is_still_alive(
    quick_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    disposed = count_dispose(quick_app, monkeypatch)
    release = asyncio.Event()

    async def stubborn() -> None:
        try:
            await release.wait()
        except asyncio.CancelledError:
            await asyncio.wait_for(release.wait(), GUARD_SECONDS)  # trì hoãn có giới hạn
            raise

    task = BACKGROUND_TASKS.spawn(stubborn(), name="media-cleanup")
    try:
        async with quick_app.router.lifespan_context(quick_app):
            pass

        assert disposed == []  # task còn có thể dùng session → KHÔNG dispose
        assert task in BACKGROUND_TASKS.pending()
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), GUARD_SECONDS)


async def test_media_pending_cleanups_reads_the_shared_registry() -> None:
    release = asyncio.Event()
    task = BACKGROUND_TASKS.spawn(release.wait(), name="media-cleanup")
    other = BACKGROUND_TASKS.spawn(release.wait(), name="video-requeue")
    try:
        assert pending_cleanups() == (task,)  # chỉ task dọn dẹp của media
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(task, other), GUARD_SECONDS)
