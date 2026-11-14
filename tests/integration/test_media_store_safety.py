"""P2-4.1: lưu ảnh an toàn trước cancellation và COMMIT không xác định.

Bất biến: chỉ hoàn hạn mức khi CHẮC CHẮN file đích không còn và không có bản ghi đã commit tham
chiếu tới nó; không xác định được → giữ file + hạn mức và log "cần đối soát". Thread thật,
`threading.Event` / `asyncio.Event` có timeout; không dùng sleep để đồng bộ.
"""

import asyncio
import io
import os
import threading
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.datastructures import UploadFile

from app.core.storage import FileState, LocalStorage
from app.modules.media.models import MediaAsset
from app.modules.media.service import MediaService, pending_cleanups
from app.modules.quota.service import StorageQuotaService
from app.modules.users.models import User
from tests.helpers import csrf_headers, register_and_login
from tests.image_samples import make_image

pytestmark = pytest.mark.integration

TIMEOUT_SECONDS = 10
PROBE_SECONDS = 0.2  # chỉ để xác nhận một task CHƯA kết thúc, không dùng để đồng bộ
PNG = make_image("PNG", with_metadata=False)
RECONCILE = "cần đối soát"


# --------------------------------------------------------------------------- hạ tầng test
class ThreadBlocker:
    """Chặn một hàm chạy trong thread cho tới khi test cho phép (`match` chọn lời gọi cần chặn)."""

    def __init__(
        self,
        real: Callable[..., Any],
        match: Callable[..., bool] = lambda *_: True,
        error: BaseException | None = None,
    ) -> None:
        self.real, self.match, self.error = real, match, error
        self.calls = 0
        self.entered, self.proceed, self.finished = (threading.Event() for _ in range(3))

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if not self.match(*args):
            return self.real(*args, **kwargs)
        self.calls += 1
        self.entered.set()
        try:
            if not self.proceed.wait(TIMEOUT_SECONDS):
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
        await asyncio.wait_for(self.proceed.wait(), TIMEOUT_SECONDS)


class RecordingSessionFactory:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory
        self.sessions: list[AsyncSession] = []

    def __call__(self) -> AsyncSession:
        session = self.factory()
        self.sessions.append(session)
        return session

    @property
    def store_session(self) -> AsyncSession:
        return self.sessions[0]


async def wait_thread(event: threading.Event) -> None:
    assert await asyncio.to_thread(event.wait, TIMEOUT_SECONDS)


async def wait_async(event: asyncio.Event) -> None:
    await asyncio.wait_for(event.wait(), TIMEOUT_SECONDS)


async def still_running(task: asyncio.Task[Any]) -> bool:
    _, pending = await asyncio.wait({task}, timeout=PROBE_SECONDS)
    return task in pending


async def finish_cleanups() -> None:
    tasks = pending_cleanups()
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), TIMEOUT_SECONDS)


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Đếm release / delete / rollback / close (close theo từng session)."""
    counts: dict[str, Any] = {"release": 0, "delete": 0, "rollback": 0, "closed": []}
    real_release, real_delete = StorageQuotaService.release, LocalStorage.delete
    real_rollback, real_close = AsyncSession.rollback, AsyncSession.close

    async def release(self: StorageQuotaService, *args: Any) -> bool:
        counts["release"] += 1
        return await real_release(self, *args)

    async def delete(self: LocalStorage, *args: Any) -> Any:
        counts["delete"] += 1
        return await real_delete(self, *args)

    async def rollback(self: AsyncSession) -> None:
        counts["rollback"] += 1
        await real_rollback(self)

    async def close(self: AsyncSession) -> None:
        counts["closed"].append(self)
        await real_close(self)

    monkeypatch.setattr(StorageQuotaService, "release", release)
    monkeypatch.setattr(LocalStorage, "delete", delete)
    monkeypatch.setattr(AsyncSession, "rollback", rollback)
    monkeypatch.setattr(AsyncSession, "close", close)
    return counts


@pytest.fixture
async def owner(app: FastAPI) -> int:
    async with app.state.session_factory() as session:
        user = User(email="owner@example.com", password_hash="x", full_name="Owner")
        session.add(user)
        await session.commit()
        return user.id


@pytest.fixture
async def factory(app: FastAPI) -> AsyncIterator[RecordingSessionFactory]:
    recording = RecordingSessionFactory(app.state.session_factory)
    yield recording
    await finish_cleanups()
    assert pending_cleanups() == ()


@pytest.fixture
async def service(app: FastAPI, factory: RecordingSessionFactory) -> AsyncIterator[MediaService]:
    async with app.state.session_factory() as request_session:
        yield MediaService(
            request_session, app.state.storage, app.state.image_gate, app.state.settings, factory
        )


def start_upload(service: MediaService, owner: int) -> asyncio.Task[MediaAsset]:
    return asyncio.create_task(service.upload_image(owner, UploadFile(io.BytesIO(PNG))))


def uploads(app: FastAPI) -> list[Path]:
    directory = app.state.settings.storage_root_path / "uploads"
    return sorted(directory.iterdir()) if directory.exists() else []


def temp_files(app: FastAPI) -> list[Path]:
    directory = app.state.settings.storage_root_path / "tmp"
    return sorted(directory.iterdir()) if directory.exists() else []


async def used(app: FastAPI, owner: int) -> int:
    async with app.state.session_factory() as session:
        return await StorageQuotaService(
            session, app.state.settings.storage_quota_bytes_per_user
        ).usage(owner)


async def assets(app: FastAPI) -> int:
    async with app.state.session_factory() as session:
        return (await session.scalar(select(func.count()).select_from(MediaAsset))) or 0


def closed_count(calls: dict[str, Any], session: AsyncSession) -> int:
    return sum(1 for closed in calls["closed"] if closed is session)


def is_upload_path(*args: Any) -> bool:
    return bool(args) and f"{os.sep}uploads{os.sep}" in os.fspath(args[-1])


# --------------------------------------------------------------------------- hủy khi đổi chỗ file
async def test_m1_cancel_during_replace_that_completes_keeps_file_and_quota(
    app: FastAPI,
    owner: int,
    service: MediaService,
    factory: RecordingSessionFactory,
    calls: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    replace = ThreadBlocker(os.replace)
    monkeypatch.setattr(os, "replace", replace)

    task = start_upload(service, owner)
    await wait_thread(replace.entered)
    task.cancel()
    assert await still_running(task)  # chờ thread đổi chỗ xong mới kết thúc
    replace.proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await finish_cleanups()

    [stored] = uploads(app)
    assert (calls["release"], calls["delete"]) == (0, 0)
    assert await used(app, owner) == stored.stat().st_size
    assert await assets(app) == 0
    assert RECONCILE in caplog.text and stored.name in caplog.text
    assert str(app.state.settings.storage_root_path) not in caplog.text  # chỉ ghi khóa tương đối
    assert closed_count(calls, factory.store_session) == 1


async def test_m2_cancel_during_replace_that_fails_releases_once(
    app: FastAPI,
    owner: int,
    service: MediaService,
    calls: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replace = ThreadBlocker(os.replace, error=PermissionError("file bị khóa (giả lập)"))
    monkeypatch.setattr(os, "replace", replace)

    task = start_upload(service, owner)
    await wait_thread(replace.entered)
    task.cancel()
    replace.proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await finish_cleanups()

    assert (calls["release"], calls["delete"]) == (1, 0)
    assert await used(app, owner) == 0
    assert uploads(app) == [] and temp_files(app) == []


async def test_m3_cancel_before_replace_starts_releases_once(
    app: FastAPI,
    owner: int,
    service: MediaService,
    calls: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_mkdir = Path.mkdir
    mkdir = ThreadBlocker(real_mkdir, match=lambda path, *_: path.name == "uploads")
    monkeypatch.setattr(Path, "mkdir", lambda path, *a, **kw: mkdir(path, *a, **kw))
    replace_calls: list[object] = []
    monkeypatch.setattr(os, "replace", lambda *args: replace_calls.append(args))

    task = start_upload(service, owner)
    await wait_thread(mkdir.entered)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    mkdir.proceed.set()
    await wait_thread(mkdir.finished)
    await finish_cleanups()

    assert replace_calls == []
    assert (calls["release"], calls["delete"]) == (1, 0)
    assert await used(app, owner) == 0


async def test_m4_second_cancel_while_replace_runs_keeps_quota(
    app: FastAPI,
    owner: int,
    service: MediaService,
    calls: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    replace = ThreadBlocker(os.replace)
    monkeypatch.setattr(os, "replace", replace)

    task = start_upload(service, owner)
    await wait_thread(replace.entered)
    task.cancel()
    assert await still_running(task)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await finish_cleanups()
    assert (calls["release"], calls["delete"]) == (0, 0)
    assert RECONCILE in caplog.text
    reserved = await used(app, owner)
    replace.proceed.set()  # thread chạy tiếp SAU khi request đã kết thúc
    await wait_thread(replace.finished)

    # File tạm đã bị xóa trước khi thread chạy tiếp → đổi chỗ thất bại, không có orphan. Hạn mức
    # vẫn giữ nguyên (tính THỪA, an toàn) cho tới khi đối soát (P2-8) — không bao giờ tính thiếu
    assert uploads(app) == []
    assert reserved > 0 and await used(app, owner) == reserved


# --------------------------------------------------------------------------- COMMIT không xác định
def commit_then(monkeypatch: pytest.MonkeyPatch, after: Callable[[], Any]) -> None:
    real_commit = MediaService._commit_asset

    async def patched(self: MediaService, session: AsyncSession) -> None:
        await real_commit(self, session)
        await after()

    monkeypatch.setattr(MediaService, "_commit_asset", patched)


async def test_m5_commit_succeeds_but_raises_returns_the_committed_asset(
    app: FastAPI,
    owner: int,
    service: MediaService,
    calls: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def lose_connection() -> None:
        raise ConnectionError("mất kết nối sau COMMIT (giả lập)")

    commit_then(monkeypatch, lose_connection)
    asset = await start_upload(service, owner)

    [stored] = uploads(app)
    assert (calls["release"], calls["delete"]) == (0, 0)
    assert asset.owner_id == owner and asset.storage_key == f"uploads/{stored.name}"
    assert stored.read_bytes() and await assets(app) == 1
    assert await used(app, owner) == stored.stat().st_size


async def test_m5_api_returns_201_when_commit_raised_after_succeeding(
    app: FastAPI, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await register_and_login(client)

    async def lose_connection() -> None:
        raise ConnectionError("mất kết nối sau COMMIT (giả lập)")

    commit_then(monkeypatch, lose_connection)
    response = await client.post(
        "/api/v1/media/images",
        files={"file": ("a.png", PNG, "image/png")},
        headers=csrf_headers(client),
    )
    await finish_cleanups()

    assert response.status_code == 201
    content = await client.get(f"/api/v1/media/images/{response.json()['id']}/content")
    assert content.status_code == 200


async def test_m6_commit_failure_without_commit_keeps_file_and_quota(
    app: FastAPI,
    owner: int,
    service: MediaService,
    calls: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def failing_commit(self: MediaService, session: AsyncSession) -> None:
        raise ConnectionError("COMMIT lỗi (giả lập)")

    monkeypatch.setattr(MediaService, "_commit_asset", failing_commit)
    with pytest.raises(ConnectionError):
        await start_upload(service, owner)
    await finish_cleanups()

    [stored] = uploads(app)
    assert (calls["release"], calls["delete"]) == (0, 0)
    assert await assets(app) == 0
    assert await used(app, owner) == stored.stat().st_size
    assert RECONCILE in caplog.text


@pytest.mark.parametrize("committed", [True, False], ids=["after-commit", "before-commit"])
async def test_m7_cancel_during_commit_keeps_file_and_quota(
    app: FastAPI,
    owner: int,
    service: MediaService,
    calls: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    committed: bool,
) -> None:
    blocker = AsyncBlocker()
    real_commit = MediaService._commit_asset

    async def hanging_commit(self: MediaService, session: AsyncSession) -> None:
        if committed:
            await real_commit(self, session)
        await blocker.pause()

    monkeypatch.setattr(MediaService, "_commit_asset", hanging_commit)
    task = start_upload(service, owner)
    await wait_async(blocker.entered)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await finish_cleanups()

    [stored] = uploads(app)
    assert (calls["release"], calls["delete"]) == (0, 0)
    assert await assets(app) == (1 if committed else 0)
    assert await used(app, owner) == stored.stat().st_size
    assert RECONCILE in caplog.text


async def test_m8_failed_verification_keeps_file_and_quota(
    app: FastAPI,
    owner: int,
    service: MediaService,
    calls: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def failing_commit(self: MediaService, session: AsyncSession) -> None:
        raise ConnectionError("COMMIT lỗi (giả lập)")

    async def failing_lookup(self: MediaService, owner_id: int, key: str) -> None:
        raise ConnectionError("DB không truy cập được (giả lập)")

    monkeypatch.setattr(MediaService, "_commit_asset", failing_commit)
    monkeypatch.setattr(MediaService, "_find_committed_asset", failing_lookup)
    with pytest.raises(ConnectionError, match="COMMIT lỗi"):
        await start_upload(service, owner)
    await finish_cleanups()

    [stored] = uploads(app)
    assert (calls["release"], calls["delete"]) == (0, 0)
    assert await used(app, owner) == stored.stat().st_size
    assert RECONCILE in caplog.text


async def test_verification_requires_matching_owner(
    app: FastAPI, owner: int, service: MediaService, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with app.state.session_factory() as session:
        other = User(email="other@example.com", password_hash="x", full_name="Other")
        session.add(other)
        await session.commit()
        other_id = other.id
    asset = await start_upload(service, owner)

    assert await service._find_committed_asset(owner, asset.storage_key) is not None
    assert await service._find_committed_asset(other_id, asset.storage_key) is None


# --------------------------------------------------------------------------- lỗi TRƯỚC COMMIT
async def failing_insert(*_: object, **__: object) -> None:
    raise RuntimeError("INSERT lỗi (giả lập)")


async def test_m9_failure_before_commit_deletes_file_then_releases_once(
    app: FastAPI,
    owner: int,
    service: MediaService,
    factory: RecordingSessionFactory,
    calls: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(MediaService, "_insert_asset", failing_insert)

    with pytest.raises(RuntimeError):
        await start_upload(service, owner)
    await finish_cleanups()

    assert (calls["release"], calls["delete"]) == (1, 1)
    assert uploads(app) == [] and temp_files(app) == []
    assert await used(app, owner) == 0
    assert closed_count(calls, factory.store_session) == 1


async def test_m10_failed_delete_keeps_quota(
    app: FastAPI,
    owner: int,
    service: MediaService,
    calls: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    real_unlink = os.unlink

    def failing_unlink(path: Any, *args: Any, **kwargs: Any) -> None:
        if is_upload_path(path):
            raise PermissionError("file bị khóa (giả lập)")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(MediaService, "_insert_asset", failing_insert)
    monkeypatch.setattr(os, "unlink", failing_unlink)
    with pytest.raises(RuntimeError):
        await start_upload(service, owner)
    await finish_cleanups()
    monkeypatch.undo()

    [orphan] = uploads(app)
    assert (calls["release"], calls["delete"]) == (0, 1)
    assert await used(app, owner) == orphan.stat().st_size
    assert RECONCILE in caplog.text


async def test_unconfirmed_deletion_keeps_quota(
    app: FastAPI,
    owner: int,
    service: MediaService,
    calls: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def unknown_state(self: LocalStorage, key: str) -> Any:
        return FileState.UNKNOWN

    monkeypatch.setattr(MediaService, "_insert_asset", failing_insert)
    monkeypatch.setattr(LocalStorage, "file_state", unknown_state)
    with pytest.raises(RuntimeError):
        await start_upload(service, owner)
    await finish_cleanups()

    assert (calls["release"], calls["delete"]) == (0, 1)
    assert RECONCILE in caplog.text


async def test_m11_cleanup_cancelled_during_delete_keeps_quota(
    app: FastAPI,
    owner: int,
    service: MediaService,
    factory: RecordingSessionFactory,
    calls: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    unlink = ThreadBlocker(os.unlink, match=is_upload_path)
    monkeypatch.setattr(os, "unlink", unlink)
    monkeypatch.setattr(MediaService, "_insert_asset", failing_insert)

    task = start_upload(service, owner)
    await wait_thread(unlink.entered)
    [cleanup] = pending_cleanups()
    cleanup.cancel()  # mô phỏng event loop bị tắt khi đang dọn dẹp
    with pytest.raises(asyncio.CancelledError):
        await task
    await finish_cleanups()
    unlink.proceed.set()
    await wait_thread(unlink.finished)

    assert (calls["release"], calls["delete"]) == (0, 1)
    assert await used(app, owner) > 0  # không xác nhận được việc xóa → giữ hạn mức
    assert RECONCILE in caplog.text
    assert closed_count(calls, factory.store_session) == 1


# --------------------------------------------------------------------------- hủy khi giữ chỗ
async def test_m12_cancel_during_reserve_never_releases(
    app: FastAPI,
    owner: int,
    service: MediaService,
    factory: RecordingSessionFactory,
    calls: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    blocker = AsyncBlocker()

    async def hanging_reserve(self: StorageQuotaService, *_: object) -> None:
        await blocker.pause()

    monkeypatch.setattr(StorageQuotaService, "reserve", hanging_reserve)
    task = start_upload(service, owner)
    await wait_async(blocker.entered)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await finish_cleanups()

    assert (calls["release"], calls["delete"]) == (0, 0)
    assert uploads(app) == [] and temp_files(app) == []
    assert RECONCILE in caplog.text
    assert closed_count(calls, factory.store_session) == 1


# ------------------------------------------------------------------ hủy LẶP LẠI khi dọn dẹp
async def cancel_repeatedly(task: asyncio.Task[Any], times: int = 3) -> None:
    for _ in range(times):
        task.cancel()
        await asyncio.wait({task}, timeout=PROBE_SECONDS)
    with pytest.raises(asyncio.CancelledError):
        await task


async def assert_cleanup_still_owns_session(
    calls: dict[str, Any], factory: RecordingSessionFactory
) -> None:
    assert len(pending_cleanups()) == 1  # không có task dọn dẹp thứ hai
    assert closed_count(calls, factory.store_session) == 0  # session chưa bị đóng khi còn dùng


async def test_repeated_cancel_while_rollback_runs(
    app: FastAPI,
    owner: int,
    service: MediaService,
    factory: RecordingSessionFactory,
    calls: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocker = AsyncBlocker()
    real_rollback = AsyncSession.rollback  # đã là bản đếm của fixture `calls`

    async def slow_rollback(self: AsyncSession) -> None:
        if factory.sessions and self is factory.store_session:
            await blocker.pause()
        await real_rollback(self)

    monkeypatch.setattr(AsyncSession, "rollback", slow_rollback)
    monkeypatch.setattr(MediaService, "_insert_asset", failing_insert)
    task = start_upload(service, owner)
    await wait_async(blocker.entered)
    await cancel_repeatedly(task)
    await assert_cleanup_still_owns_session(calls, factory)
    assert (blocker.calls, calls["delete"], calls["release"]) == (1, 0, 0)

    blocker.proceed.set()
    await finish_cleanups()

    assert (blocker.calls, calls["delete"], calls["release"]) == (1, 1, 1)
    assert uploads(app) == [] and await used(app, owner) == 0
    assert closed_count(calls, factory.store_session) == 1


async def test_repeated_cancel_while_delete_runs(
    app: FastAPI,
    owner: int,
    service: MediaService,
    factory: RecordingSessionFactory,
    calls: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unlink = ThreadBlocker(os.unlink, match=is_upload_path)
    monkeypatch.setattr(os, "unlink", unlink)
    monkeypatch.setattr(MediaService, "_insert_asset", failing_insert)
    task = start_upload(service, owner)
    await wait_thread(unlink.entered)
    await cancel_repeatedly(task)
    await assert_cleanup_still_owns_session(calls, factory)
    assert (calls["delete"], calls["release"]) == (1, 0)

    unlink.proceed.set()
    await finish_cleanups()

    assert (unlink.calls, calls["delete"], calls["release"]) == (1, 1, 1)
    assert uploads(app) == [] and await used(app, owner) == 0
    assert closed_count(calls, factory.store_session) == 1


async def test_repeated_cancel_while_release_runs(
    app: FastAPI,
    owner: int,
    service: MediaService,
    factory: RecordingSessionFactory,
    calls: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocker = AsyncBlocker()
    real_release = StorageQuotaService.release  # đã là bản đếm của fixture `calls`

    async def slow_release(self: StorageQuotaService, *args: Any) -> bool:
        await blocker.pause()
        return await real_release(self, *args)

    monkeypatch.setattr(StorageQuotaService, "release", slow_release)
    monkeypatch.setattr(MediaService, "_insert_asset", failing_insert)
    task = start_upload(service, owner)
    await wait_async(blocker.entered)
    await cancel_repeatedly(task)
    await assert_cleanup_still_owns_session(calls, factory)
    assert (blocker.calls, calls["release"]) == (1, 0)

    blocker.proceed.set()
    await finish_cleanups()

    assert (blocker.calls, calls["release"]) == (1, 1)  # đúng MỘT lần hoàn hạn mức
    assert await used(app, owner) == 0
    assert closed_count(calls, factory.store_session) == 1


# --------------------------------------------------------------------------- đường bình thường
async def test_successful_upload_closes_store_session_once(
    app: FastAPI,
    owner: int,
    service: MediaService,
    factory: RecordingSessionFactory,
    calls: dict[str, Any],
) -> None:
    asset = await start_upload(service, owner)

    [stored] = uploads(app)
    assert asset.storage_key == f"uploads/{stored.name}"
    assert (calls["release"], calls["delete"]) == (0, 0)
    assert await used(app, owner) == stored.stat().st_size == asset.size_bytes
    assert closed_count(calls, factory.store_session) == 1
    assert pending_cleanups() == ()
