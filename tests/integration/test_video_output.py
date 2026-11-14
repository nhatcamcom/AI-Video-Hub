"""Lưu video đầu ra theo hợp đồng quota 4.2 (P2-7, W9). Đếm release / delete trong mọi nhánh.

Thread thật + `threading.Event` / `asyncio.Event` có timeout; không dùng sleep để đồng bộ.
"""

import asyncio
import os
import threading
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.background_tasks import BACKGROUND_TASKS
from app.core.config import Settings
from app.core.storage import LocalStorage, TempFile
from app.modules.quota.service import StorageQuotaService
from app.modules.users.models import User
from app.modules.videos.jobs import VideoJobService
from app.modules.videos.models import Video, VideoStatus
from app.modules.videos.output import (
    CLEANUP_TASK_NAME,
    StoreOutcome,
    VideoOutputStore,
)
from app.modules.videos.providers.base import VideoJob

pytestmark = pytest.mark.integration

TIMEOUT = 10
PROBE = 0.2
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x01" * 2000
RECONCILE = "cần đối soát"


class ThreadBlocker:
    def __init__(
        self,
        real: Callable[..., Any],
        match: Callable[..., bool] = lambda *_: True,
        error: BaseException | None = None,
    ) -> None:
        self.real, self.match, self.error, self.calls = real, match, error, 0
        self.entered, self.proceed, self.finished = (threading.Event() for _ in range(3))

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if not self.match(*args):
            return self.real(*args, **kwargs)
        self.calls += 1
        self.entered.set()
        try:
            if not self.proceed.wait(TIMEOUT):
                raise TimeoutError("test không cho thread chạy tiếp")
            if self.error is not None:
                raise self.error
            return self.real(*args, **kwargs)
        finally:
            self.finished.set()


class AsyncBlocker:
    def __init__(self) -> None:
        self.calls = 0
        self.entered, self.proceed = asyncio.Event(), asyncio.Event()

    async def pause(self) -> None:
        self.calls += 1
        self.entered.set()
        await asyncio.wait_for(self.proceed.wait(), TIMEOUT)


async def wait_thread(event: threading.Event) -> None:
    assert await asyncio.to_thread(event.wait, TIMEOUT)


async def still_running(task: asyncio.Task[Any]) -> bool:
    _, pending = await asyncio.wait({task}, timeout=PROBE)
    return task in pending


def cleanups() -> tuple[asyncio.Task[Any], ...]:
    return tuple(t for t in BACKGROUND_TASKS.pending() if t.get_name() == CLEANUP_TASK_NAME)


async def finish_cleanups() -> None:
    if tasks := cleanups():
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), TIMEOUT)


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    counts: dict[str, Any] = {"release": 0, "delete": 0, "closed": []}
    real_release, real_delete, real_close = (
        StorageQuotaService.release,
        LocalStorage.delete,
        AsyncSession.close,
    )

    async def release(self: StorageQuotaService, *args: Any) -> bool:
        counts["release"] += 1
        return await real_release(self, *args)

    async def delete(self: LocalStorage, *args: Any) -> Any:
        counts["delete"] += 1
        return await real_delete(self, *args)

    async def close(self: AsyncSession) -> None:
        counts["closed"].append(self)
        await real_close(self)

    monkeypatch.setattr(StorageQuotaService, "release", release)
    monkeypatch.setattr(LocalStorage, "delete", delete)
    monkeypatch.setattr(AsyncSession, "close", close)
    return counts


@pytest.fixture
async def jobs(app: FastAPI, settings: Settings) -> AsyncIterator[VideoJobService]:
    yield VideoJobService(app.state.session_factory, settings)
    await finish_cleanups()
    assert cleanups() == ()


class RecordingSessionFactory:
    """Ghi lại session do VideoOutputStore tạo (session lưu là session đầu tiên)."""

    def __init__(self, factory: Any) -> None:
        self.factory = factory
        self.sessions: list[AsyncSession] = []

    def __call__(self) -> AsyncSession:
        session: AsyncSession = self.factory()
        self.sessions.append(session)
        return session


@pytest.fixture
def factory(app: FastAPI) -> RecordingSessionFactory:
    return RecordingSessionFactory(app.state.session_factory)


@pytest.fixture
def store(
    app: FastAPI, settings: Settings, jobs: VideoJobService, factory: RecordingSessionFactory
) -> VideoOutputStore:
    return VideoOutputStore(factory, app.state.storage, settings, jobs)  # type: ignore[arg-type]


def store_closes(calls: dict[str, Any], factory: RecordingSessionFactory) -> int:
    return sum(1 for closed in calls["closed"] if closed is factory.sessions[0])


@pytest.fixture
async def job(app: FastAPI, jobs: VideoJobService) -> VideoJob:
    async with app.state.session_factory() as session:
        user = User(email="owner@example.com", password_hash="x", full_name="Owner")
        session.add(user)
        await session.commit()
        session.add(
            Video(
                owner_id=user.id,
                prompt="prompt bí mật",
                aspect_ratio="16:9",
                duration_seconds=5,
                model_key="mock",
                status=VideoStatus.QUEUED,
                active_slot=1,
            )
        )
        await session.commit()
    claimed = await jobs.claim_next()
    assert claimed is not None
    return claimed


async def temp_output(app: FastAPI) -> TempFile:
    async def chunks() -> AsyncIterator[bytes]:
        yield MP4

    temp: TempFile = await app.state.storage.write_temp_file(chunks())
    return temp


def videos_dir(app: FastAPI) -> list[Path]:
    directory = app.state.settings.storage_root_path / "videos"
    return sorted(directory.iterdir()) if directory.exists() else []


def temp_dir(app: FastAPI) -> list[Path]:
    directory = app.state.settings.storage_root_path / "tmp"
    return sorted(directory.iterdir()) if directory.exists() else []


async def used(app: FastAPI, owner: int) -> int:
    async with app.state.session_factory() as session:
        return await StorageQuotaService(
            session, app.state.settings.storage_quota_bytes_per_user
        ).usage(owner)


async def video_row(app: FastAPI, video_id: int) -> Video:
    async with app.state.session_factory() as session:
        video: Video | None = await session.get(Video, video_id)
        assert video is not None
        return video


def is_video_path(*args: Any) -> bool:
    return bool(args) and f"{os.sep}videos{os.sep}" in os.fspath(args[-1])


# --------------------------------------------------------------------------- đường bình thường
async def test_store_succeeds_and_counts_real_bytes(
    app: FastAPI,
    store: VideoOutputStore,
    job: VideoJob,
    calls: dict[str, Any],
    factory: RecordingSessionFactory,
) -> None:
    outcome = await store.store(job, await temp_output(app))

    [stored] = videos_dir(app)
    row = await video_row(app, job.id)
    assert outcome is StoreOutcome.STORED
    assert (row.status, row.progress, row.active_slot) == (VideoStatus.SUCCEEDED, 100, None)
    assert row.output_storage_key == f"videos/{stored.name}" and stored.suffix == ".mp4"
    assert row.output_size_bytes == len(MP4) == await used(app, job.owner_id)
    assert (calls["release"], calls["delete"]) == (0, 0)
    assert temp_dir(app) == [] and store_closes(calls, factory) == 1


async def test_quota_exceeded_keeps_nothing(
    app: FastAPI, store: VideoOutputStore, job: VideoJob, calls: dict[str, Any]
) -> None:
    async with app.state.session_factory() as session:
        quota = app.state.settings.storage_quota_bytes_per_user
        await StorageQuotaService(session, quota).set_used(job.owner_id, quota - 10)

    outcome = await store.store(job, await temp_output(app))

    assert outcome is StoreOutcome.QUOTA_EXCEEDED
    assert videos_dir(app) == [] and temp_dir(app) == []
    assert (calls["release"], calls["delete"]) == (0, 0)
    assert (await video_row(app, job.id)).status == VideoStatus.PROCESSING  # runner quyết định


async def test_cancel_during_reserve_never_releases(
    app: FastAPI,
    store: VideoOutputStore,
    job: VideoJob,
    calls: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    blocker = AsyncBlocker()

    async def hanging_reserve(self: StorageQuotaService, *_: object) -> None:
        await blocker.pause()

    monkeypatch.setattr(StorageQuotaService, "reserve", hanging_reserve)
    task = asyncio.create_task(store.store(job, await temp_output(app)))
    await asyncio.wait_for(blocker.entered.wait(), TIMEOUT)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await finish_cleanups()

    assert (calls["release"], calls["delete"]) == (0, 0)
    assert temp_dir(app) == [] and RECONCILE in caplog.text


# --------------------------------------------------------------------------- hủy khi đổi chỗ file
@pytest.mark.parametrize(
    ("thread_error", "expected_release", "expected_files"),
    [(None, 0, 1), (PermissionError("file bị khóa (giả lập)"), 1, 0)],
    ids=["replace-completes", "replace-fails"],
)
async def test_cancel_during_replace_follows_the_real_outcome(
    app: FastAPI,
    store: VideoOutputStore,
    job: VideoJob,
    calls: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    thread_error: BaseException | None,
    expected_release: int,
    expected_files: int,
) -> None:
    replace = ThreadBlocker(os.replace, error=thread_error)
    monkeypatch.setattr(os, "replace", replace)

    task = asyncio.create_task(store.store(job, await temp_output(app)))
    await wait_thread(replace.entered)
    task.cancel()
    assert await still_running(task)  # chờ thread đổi chỗ xong mới quyết định
    replace.proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await finish_cleanups()

    assert (calls["release"], calls["delete"]) == (expected_release, 0)
    assert len(videos_dir(app)) == expected_files
    expected_used = len(MP4) if expected_files else 0
    assert await used(app, job.owner_id) == expected_used
    assert (await video_row(app, job.id)).status == VideoStatus.PROCESSING  # không tự requeue


async def test_cancel_before_replace_starts_releases_once(
    app: FastAPI,
    store: VideoOutputStore,
    job: VideoJob,
    calls: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mkdir = ThreadBlocker(Path.mkdir, match=lambda path, *_: path.name == "videos")
    monkeypatch.setattr(Path, "mkdir", lambda path, *a, **kw: mkdir(path, *a, **kw))

    task = asyncio.create_task(store.store(job, await temp_output(app)))
    await wait_thread(mkdir.entered)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    mkdir.proceed.set()
    await wait_thread(mkdir.finished)
    await finish_cleanups()

    assert (calls["release"], calls["delete"]) == (1, 0)
    assert await used(app, job.owner_id) == 0


async def test_second_cancel_during_replace_keeps_quota(
    app: FastAPI,
    store: VideoOutputStore,
    job: VideoJob,
    calls: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    replace = ThreadBlocker(os.replace)
    monkeypatch.setattr(os, "replace", replace)

    task = asyncio.create_task(store.store(job, await temp_output(app)))
    await wait_thread(replace.entered)
    task.cancel()
    assert await still_running(task)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await finish_cleanups()
    replace.proceed.set()
    await wait_thread(replace.finished)

    assert (calls["release"], calls["delete"]) == (0, 0)
    assert await used(app, job.owner_id) == len(MP4)  # giữ (tính thừa nếu không có file)
    assert RECONCILE in caplog.text


# --------------------------------------------------------------------------- CAS hoàn tất (execute)
async def test_lost_lease_deletes_file_then_releases_once(
    app: FastAPI, store: VideoOutputStore, job: VideoJob, calls: dict[str, Any]
) -> None:
    async with app.state.session_factory() as session:  # worker khác đã lấy job
        await session.execute(update(Video).values(lease_token="e" * 32))
        await session.commit()

    outcome = await store.store(job, await temp_output(app))
    await finish_cleanups()

    assert outcome is StoreOutcome.LEASE_LOST
    assert (calls["release"], calls["delete"]) == (1, 1)
    assert videos_dir(app) == [] and await used(app, job.owner_id) == 0
    row = await video_row(app, job.id)
    assert row.status == VideoStatus.PROCESSING and row.output_storage_key is None


async def test_record_error_before_commit_deletes_then_releases(
    app: FastAPI,
    store: VideoOutputStore,
    job: VideoJob,
    calls: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing_record(*_: object) -> None:
        raise RuntimeError("UPDATE lỗi (giả lập)")

    monkeypatch.setattr(VideoOutputStore, "_record", failing_record)
    with pytest.raises(RuntimeError):
        await store.store(job, await temp_output(app))
    await finish_cleanups()

    assert (calls["release"], calls["delete"]) == (1, 1)
    assert videos_dir(app) == [] and await used(app, job.owner_id) == 0


async def test_record_error_with_failed_delete_keeps_quota(
    app: FastAPI,
    store: VideoOutputStore,
    job: VideoJob,
    calls: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    real_unlink = os.unlink

    def failing_unlink(path: Any, *args: Any, **kwargs: Any) -> None:
        if is_video_path(path):
            raise PermissionError("file bị khóa (giả lập)")
        real_unlink(path, *args, **kwargs)

    async def failing_record(*_: object) -> None:
        raise RuntimeError("UPDATE lỗi (giả lập)")

    monkeypatch.setattr(VideoOutputStore, "_record", failing_record)
    monkeypatch.setattr(os, "unlink", failing_unlink)
    with pytest.raises(RuntimeError):
        await store.store(job, await temp_output(app))
    await finish_cleanups()
    monkeypatch.undo()

    assert (calls["release"], calls["delete"]) == (0, 1)
    assert len(videos_dir(app)) == 1 and await used(app, job.owner_id) == len(MP4)
    assert RECONCILE in caplog.text


# --------------------------------------------------------------------------- COMMIT không xác định
def commit_then(monkeypatch: pytest.MonkeyPatch, after: Callable[[], Any]) -> None:
    real_commit = VideoOutputStore._commit

    async def patched(self: VideoOutputStore, session: AsyncSession) -> None:
        await real_commit(self, session)
        await after()

    monkeypatch.setattr(VideoOutputStore, "_commit", patched)


async def test_commit_succeeded_but_raised_is_treated_as_stored(
    app: FastAPI,
    store: VideoOutputStore,
    job: VideoJob,
    calls: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def lose_connection() -> None:
        raise ConnectionError("mất kết nối sau COMMIT (giả lập)")

    commit_then(monkeypatch, lose_connection)
    outcome = await store.store(job, await temp_output(app))
    await finish_cleanups()

    [stored] = videos_dir(app)
    assert outcome is StoreOutcome.STORED
    assert (calls["release"], calls["delete"]) == (0, 0)
    assert stored.read_bytes() == MP4 and (await video_row(app, job.id)).status == "succeeded"


async def test_commit_failure_without_commit_keeps_file_and_quota(
    app: FastAPI,
    store: VideoOutputStore,
    job: VideoJob,
    calls: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def failing_commit(self: VideoOutputStore, session: AsyncSession) -> None:
        raise ConnectionError("COMMIT lỗi (giả lập)")

    monkeypatch.setattr(VideoOutputStore, "_commit", failing_commit)
    with pytest.raises(ConnectionError):
        await store.store(job, await temp_output(app))
    await finish_cleanups()

    assert (calls["release"], calls["delete"]) == (0, 0)
    assert len(videos_dir(app)) == 1 and await used(app, job.owner_id) == len(MP4)
    assert (await video_row(app, job.id)).status == VideoStatus.PROCESSING  # reaper xử lý sau
    assert RECONCILE in caplog.text


@pytest.mark.parametrize("committed", [True, False], ids=["after-commit", "before-commit"])
async def test_cancel_during_commit_keeps_file_and_quota(
    app: FastAPI,
    store: VideoOutputStore,
    job: VideoJob,
    calls: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    committed: bool,
) -> None:
    blocker = AsyncBlocker()
    real_commit = VideoOutputStore._commit

    async def hanging_commit(self: VideoOutputStore, session: AsyncSession) -> None:
        if committed:
            await real_commit(self, session)
        await blocker.pause()

    monkeypatch.setattr(VideoOutputStore, "_commit", hanging_commit)
    task = asyncio.create_task(store.store(job, await temp_output(app)))
    await asyncio.wait_for(blocker.entered.wait(), TIMEOUT)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await finish_cleanups()

    assert (calls["release"], calls["delete"]) == (0, 0)
    assert len(videos_dir(app)) == 1 and await used(app, job.owner_id) == len(MP4)
    expected = VideoStatus.SUCCEEDED if committed else VideoStatus.PROCESSING
    assert (await video_row(app, job.id)).status == expected


async def test_failed_verification_keeps_file_and_quota(
    app: FastAPI,
    store: VideoOutputStore,
    job: VideoJob,
    calls: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def failing_commit(self: VideoOutputStore, session: AsyncSession) -> None:
        raise ConnectionError("COMMIT lỗi (giả lập)")

    async def failing_lookup(self: VideoJobService, *_: object) -> bool:
        raise ConnectionError("DB không truy cập được (giả lập)")

    monkeypatch.setattr(VideoOutputStore, "_commit", failing_commit)
    monkeypatch.setattr(VideoJobService, "find_stored", failing_lookup)
    with pytest.raises(ConnectionError, match="COMMIT lỗi"):
        await store.store(job, await temp_output(app))
    await finish_cleanups()

    assert (calls["release"], calls["delete"]) == (0, 0)
    assert RECONCILE in caplog.text


# ------------------------------------------------------------------ hủy LẶP LẠI khi dọn dẹp
async def test_repeated_cancel_while_release_runs_releases_exactly_once(
    app: FastAPI,
    store: VideoOutputStore,
    job: VideoJob,
    calls: dict[str, Any],
    factory: RecordingSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocker = AsyncBlocker()
    counting_release = StorageQuotaService.release

    async def slow_release(self: StorageQuotaService, *args: Any) -> bool:
        await blocker.pause()
        return await counting_release(self, *args)

    async with app.state.session_factory() as session:  # mất lease → nhánh xóa + hoàn
        await session.execute(update(Video).values(lease_token="e" * 32))
        await session.commit()
    monkeypatch.setattr(StorageQuotaService, "release", slow_release)
    task = asyncio.create_task(store.store(job, await temp_output(app)))
    try:
        await asyncio.wait_for(blocker.entered.wait(), TIMEOUT)
        for _ in range(3):
            task.cancel()
            await asyncio.wait({task}, timeout=PROBE)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(cleanups()) == 1  # không có task dọn dẹp thứ hai
        assert store_closes(calls, factory) == 0  # session chưa đóng khi task còn dùng
    finally:
        blocker.proceed.set()
    await finish_cleanups()

    assert (blocker.calls, calls["release"], calls["delete"]) == (1, 1, 1)
    assert await used(app, job.owner_id) == 0 and store_closes(calls, factory) == 1


async def test_cleanup_cancelled_during_delete_keeps_quota_and_closes_session(
    app: FastAPI,
    store: VideoOutputStore,
    job: VideoJob,
    calls: dict[str, Any],
    factory: RecordingSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    unlink = ThreadBlocker(os.unlink, match=is_video_path)
    monkeypatch.setattr(os, "unlink", unlink)
    async with app.state.session_factory() as session:  # mất lease → nhánh xóa + hoàn
        await session.execute(update(Video).values(lease_token="e" * 32))
        await session.commit()

    task = asyncio.create_task(store.store(job, await temp_output(app)))
    try:
        await wait_thread(unlink.entered)
        [cleanup] = cleanups()
        cleanup.cancel()  # mô phỏng event loop bị tắt khi đang dọn dẹp
        with pytest.raises(asyncio.CancelledError):
            await task
        await finish_cleanups()
    finally:
        unlink.proceed.set()
    await wait_thread(unlink.finished)

    assert (calls["release"], calls["delete"]) == (0, 1)  # không xác nhận được xóa → giữ hạn mức
    assert await used(app, job.owner_id) == len(MP4)
    assert RECONCILE in caplog.text and "delete" in caplog.text
    assert store_closes(calls, factory) == 1
