"""VideoWorker (P2-7): xử lý job đầu-cuối, lỗi / retry, mất lease, hủy theo giai đoạn, dừng an toàn.

Dùng DB SQLite tạm + storage tạm; MockProvider sao chép file MP4 giả trong tmp_path.
"""

import asyncio
import errno
import os
import signal
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.background_tasks import BACKGROUND_TASKS
from app.core.config import Settings
from app.core.database import Base, create_engine, create_session_factory
from app.core.storage import LocalStorage, TempFile, create_storage
from app.core.time import utc_now
from app.modules.media.models import MediaAsset
from app.modules.quota.service import StorageQuotaService
from app.modules.users.models import User
from app.modules.videos.catalog import (
    MOCK_VIDEO_MODEL,
    AspectRatio,
    VideoModelSpec,
    build_video_model_catalog,
)
from app.modules.videos.jobs import (
    INPUT_IMAGE_UNAVAILABLE,
    PROCESSING_FAILED,
    STORAGE_QUOTA_EXCEEDED,
    VIDEO_MODEL_UNAVAILABLE,
    VideoJobService,
)
from app.modules.videos.models import Video, VideoStatus
from app.modules.videos.output import VideoOutputStore
from app.modules.videos.providers.base import (
    MOCK_FORCED_FAILURE,
    MOCK_SAMPLE_MISSING,
    ProgressReporter,
    VideoJob,
)
from app.modules.videos.providers.registry import build_provider_registry
from app.modules.videos.runner import VideoWorker, image_checker
from app.worker import install_signal_handlers, run_worker
from tests.conftest import build_settings, sqlite_file_url

pytestmark = pytest.mark.integration

TIMEOUT = 15
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x07" * 4096


@dataclass
class Env:
    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    storage: LocalStorage
    jobs: VideoJobService


async def make_env(tmp_path: Path, **overrides: Any) -> Env:
    sample = tmp_path / "sample.mp4"
    sample.write_bytes(MP4)
    values: dict[str, Any] = {
        "database_url": sqlite_file_url(tmp_path),
        "storage_root": str(tmp_path / "storage"),
        "mock_sample_video_path": str(sample),
        "mock_processing_seconds": 0,
        "worker_poll_seconds": 0.05,
        "worker_shutdown_grace_seconds": 0.2,
        "shutdown_drain_seconds": 2,
        "shutdown_cancel_grace_seconds": 1,
        **overrides,
    }
    settings = build_settings(**values)
    engine = create_engine(settings)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)
    return Env(
        settings,
        engine,
        session_factory,
        create_storage(settings),
        VideoJobService(session_factory, settings),
    )


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    environment = await make_env(tmp_path)
    yield environment
    await settle_background()
    await environment.engine.dispose()


async def settle_background() -> None:
    if pending := BACKGROUND_TASKS.pending():
        await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), TIMEOUT)


def make_worker(
    env: Env, *, providers: Any = None, catalog: Any = None, **kwargs: Any
) -> VideoWorker:
    catalog = catalog or build_video_model_catalog(env.settings.enabled_video_models)
    providers = providers or build_provider_registry(catalog, env.settings, env.storage)
    return VideoWorker(
        settings=env.settings,
        jobs=env.jobs,
        output=VideoOutputStore(env.session_factory, env.storage, env.settings, env.jobs),
        catalog=catalog,
        providers=providers,
        image_available=image_checker(env.session_factory, env.storage, env.settings),
        **kwargs,
    )


async def add_job(env: Env, **overrides: Any) -> tuple[int, int]:
    async with env.session_factory() as session:
        user = User(email=f"u{os.urandom(4).hex()}@example.com", password_hash="x", full_name="U")
        session.add(user)
        await session.commit()
        video = Video(
            owner_id=user.id,
            prompt="prompt-bi-mat-cua-user",
            aspect_ratio="16:9",
            duration_seconds=5,
            model_key="mock",
            status=VideoStatus.QUEUED,
            active_slot=1,
            **overrides,
        )
        session.add(video)
        await session.commit()
        return user.id, video.id


async def video_row(env: Env, video_id: int) -> Video:
    async with env.session_factory() as session:
        video = await session.get(Video, video_id)
        assert video is not None
        return video


async def used(env: Env, owner: int) -> int:
    async with env.session_factory() as session:
        return await StorageQuotaService(session, env.settings.storage_quota_bytes_per_user).usage(
            owner
        )


def videos_dir(env: Env) -> list[Path]:
    directory = env.settings.storage_root_path / "videos"
    return sorted(directory.iterdir()) if directory.exists() else []


class FakeProvider:
    """Provider giả: chạy `behaviour(job, report)` để mô phỏng lỗi / chặn."""

    def __init__(
        self, behaviour: Callable[[VideoJob, ProgressReporter], Awaitable[TempFile]]
    ) -> None:
        self.behaviour, self.calls = behaviour, 0

    async def generate(self, job: VideoJob, report_progress: ProgressReporter) -> TempFile:
        self.calls += 1
        return await self.behaviour(job, report_progress)


# --------------------------------------------------------------------------- đầu-cuối
async def test_worker_processes_a_job_end_to_end(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    owner, video_id = await add_job(env)

    await asyncio.wait_for(make_worker(env, exit_when_idle=True).run(), TIMEOUT)

    row = await video_row(env, video_id)
    [stored] = videos_dir(env)
    assert (row.status, row.progress, row.active_slot, row.attempt_count) == (
        VideoStatus.SUCCEEDED,
        100,
        None,
        1,
    )
    assert row.output_storage_key == f"videos/{stored.name}" and stored.read_bytes() == MP4
    assert row.output_size_bytes == len(MP4) == await used(env, owner)
    assert row.lease_token is None and row.finished_at is not None
    assert "prompt-bi-mat-cua-user" not in caplog.text  # không ghi prompt vào log


async def test_worker_processes_queued_jobs_in_order(env: Env) -> None:
    ids = [(await add_job(env))[1] for _ in range(3)]

    await asyncio.wait_for(make_worker(env, exit_when_idle=True).run(), TIMEOUT)

    assert [(await video_row(env, video_id)).status for video_id in ids] == ["succeeded"] * 3


# --------------------------------------------------------------------------- lỗi không thử lại
@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"mock_sample_video_path": ""}, MOCK_SAMPLE_MISSING),
        ({"mock_force_failure": True}, MOCK_FORCED_FAILURE),
    ],
    ids=["sample-missing", "forced-failure"],
)
async def test_mock_errors_fail_immediately_without_retry(
    tmp_path: Path, overrides: dict[str, Any], code: str
) -> None:
    env = await make_env(tmp_path, **overrides)
    try:
        _, video_id = await add_job(env)
        await asyncio.wait_for(make_worker(env, exit_when_idle=True).run(), TIMEOUT)
        row = await video_row(env, video_id)
    finally:
        await env.engine.dispose()

    assert (row.status, row.error_code, row.attempt_count, row.active_slot) == (
        VideoStatus.FAILED,
        code,
        1,
        None,
    )


async def test_disabled_model_fails_the_job(env: Env) -> None:
    other = VideoModelSpec(
        key="other",
        display_name="Other",
        is_demo=True,
        aspect_ratios=(AspectRatio.LANDSCAPE,),
        durations_seconds=(5,),
        supports_input_image=False,
    )
    catalog = build_video_model_catalog(["other"], known=(MOCK_VIDEO_MODEL, other))
    providers = {"other": FakeProvider(lambda *_: asyncio.sleep(0))}  # type: ignore[arg-type]
    _, video_id = await add_job(env)

    await asyncio.wait_for(
        make_worker(env, catalog=catalog, providers=providers, exit_when_idle=True).run(), TIMEOUT
    )

    row = await video_row(env, video_id)
    assert (row.status, row.error_code) == (VideoStatus.FAILED, VIDEO_MODEL_UNAVAILABLE)
    assert providers["other"].calls == 0


async def add_image(env: Env, owner: int, *, deleted: bool) -> int:
    async with env.session_factory() as session:
        image = MediaAsset(
            owner_id=owner,
            storage_key="uploads/" + os.urandom(16).hex() + ".png",
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


@pytest.mark.parametrize("deleted", [True, False], ids=["deleted", "present"])
async def test_input_image_is_checked_when_the_job_is_claimed(env: Env, deleted: bool) -> None:
    owner, video_id = await add_job(env)
    image_id = await add_image(env, owner, deleted=deleted)
    async with env.session_factory() as session:
        await session.execute(
            update(Video).where(Video.id == video_id).values(input_image_id=image_id)
        )
        await session.commit()

    await asyncio.wait_for(make_worker(env, exit_when_idle=True).run(), TIMEOUT)

    row = await video_row(env, video_id)
    if deleted:
        assert (row.status, row.error_code) == (VideoStatus.FAILED, INPUT_IMAGE_UNAVAILABLE)
    else:
        assert row.status == VideoStatus.SUCCEEDED


async def test_quota_exceeded_fails_the_job_and_frees_the_slot(env: Env) -> None:
    owner, video_id = await add_job(env)
    async with env.session_factory() as session:
        quota = env.settings.storage_quota_bytes_per_user
        await StorageQuotaService(session, quota).set_used(owner, quota - 10)

    await asyncio.wait_for(make_worker(env, exit_when_idle=True).run(), TIMEOUT)

    row = await video_row(env, video_id)
    assert (row.status, row.error_code, row.active_slot) == (
        VideoStatus.FAILED,
        STORAGE_QUOTA_EXCEEDED,
        None,
    )
    assert videos_dir(env) == []


# --------------------------------------------------------------------------- retry (W5)
@pytest.mark.parametrize(
    "error",
    [OperationalError("SELECT", {}, Exception("database is locked")), OSError(errno.EIO, "I/O")],
    ids=["db-locked", "eio"],
)
async def test_transient_errors_are_retried_until_attempts_run_out(
    env: Env, error: BaseException
) -> None:
    async def flaky(*_: object) -> TempFile:
        raise error

    provider = FakeProvider(flaky)
    _, video_id = await add_job(env)
    worker = make_worker(env, providers={"mock": provider}, exit_when_idle=True)

    for attempt in range(1, env.settings.worker_max_attempts):
        await asyncio.wait_for(worker.run(), TIMEOUT)
        row = await video_row(env, video_id)
        assert (row.status, row.attempt_count) == (VideoStatus.QUEUED, attempt)
        assert row.next_attempt_at is not None and row.active_slot == 1
        async with env.session_factory() as session:  # bỏ qua thời gian chờ backoff
            await session.execute(update(Video).values(next_attempt_at=None))
            await session.commit()
    await asyncio.wait_for(worker.run(), TIMEOUT)

    row = await video_row(env, video_id)
    assert (row.status, row.error_code, row.attempt_count) == (
        VideoStatus.FAILED,
        PROCESSING_FAILED,
        env.settings.worker_max_attempts,
    )
    assert provider.calls == env.settings.worker_max_attempts


async def test_programming_error_fails_on_the_first_attempt(env: Env) -> None:
    async def buggy(*_: object) -> TempFile:
        raise ValueError("bug")

    _, video_id = await add_job(env)
    await asyncio.wait_for(
        make_worker(env, providers={"mock": FakeProvider(buggy)}, exit_when_idle=True).run(),
        TIMEOUT,
    )

    row = await video_row(env, video_id)
    assert (row.status, row.error_code, row.attempt_count) == (
        VideoStatus.FAILED,
        PROCESSING_FAILED,
        1,
    )


async def test_db_error_while_claiming_does_not_crash_the_worker(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, video_id = await add_job(env)
    real_claim, failures = VideoJobService.claim_next, [0]

    async def flaky_claim(self: VideoJobService) -> VideoJob | None:
        if failures[0] == 0:
            failures[0] += 1
            raise OperationalError("UPDATE", {}, Exception("database is locked"))
        return await real_claim(self)

    monkeypatch.setattr(VideoJobService, "claim_next", flaky_claim)
    await asyncio.wait_for(make_worker(env, exit_when_idle=True).run(), TIMEOUT)

    assert (await video_row(env, video_id)).status == VideoStatus.SUCCEEDED


# --------------------------------------------------------------------------- mất lease
async def test_lost_lease_cancels_the_provider_and_leaves_the_job_alone(tmp_path: Path) -> None:
    env = await make_env(tmp_path, worker_heartbeat_seconds=0.05, worker_lease_seconds=5)
    started, provider_cancelled = asyncio.Event(), asyncio.Event()

    async def blocking(*_: object) -> TempFile:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            provider_cancelled.set()
            raise
        raise AssertionError("không tới đây")

    try:
        _, video_id = await add_job(env)
        worker = make_worker(env, providers={"mock": FakeProvider(blocking)}, exit_when_idle=True)
        run = asyncio.create_task(worker.run())
        await asyncio.wait_for(started.wait(), TIMEOUT)
        async with env.session_factory() as session:  # worker khác đã lấy job
            await session.execute(update(Video).values(lease_token="e" * 32))
            await session.commit()
        await asyncio.wait_for(provider_cancelled.wait(), TIMEOUT)
        await asyncio.wait_for(run, TIMEOUT)
        row = await video_row(env, video_id)
    finally:
        await env.engine.dispose()

    assert (row.status, row.lease_token, row.output_storage_key) == (
        VideoStatus.PROCESSING,
        "e" * 32,
        None,
    )


# --------------------------------------------------------------------- dừng an toàn (W11, W12)
def blocking_provider(
    storage: LocalStorage, started: asyncio.Event, release: asyncio.Event
) -> FakeProvider:
    async def behaviour(job: VideoJob, report: ProgressReporter) -> TempFile:
        started.set()
        await release.wait()

        async def chunks() -> AsyncIterator[bytes]:
            yield MP4

        return await storage.write_temp_file(chunks())

    return FakeProvider(behaviour)


async def test_stop_while_idle_returns_promptly(env: Env) -> None:
    worker = make_worker(env)
    run = asyncio.create_task(worker.run())
    await asyncio.sleep(0.1)

    worker.request_stop()

    await asyncio.wait_for(run, TIMEOUT)


async def test_stop_waits_for_a_job_that_finishes_within_the_grace_period(tmp_path: Path) -> None:
    env = await make_env(tmp_path, worker_shutdown_grace_seconds=5)
    started, release = asyncio.Event(), asyncio.Event()
    try:
        _, video_id = await add_job(env)
        worker = make_worker(
            env, providers={"mock": blocking_provider(env.storage, started, release)}
        )
        run = asyncio.create_task(worker.run())
        await asyncio.wait_for(started.wait(), TIMEOUT)
        worker.request_stop()
        asyncio.get_running_loop().call_later(0.1, release.set)
        await asyncio.wait_for(run, TIMEOUT)
        row = await video_row(env, video_id)
    finally:
        release.set()
        await settle_background()
        await env.engine.dispose()

    assert row.status == VideoStatus.SUCCEEDED


async def test_stop_after_grace_requeues_provider_stage_job_without_counting_the_attempt(
    env: Env,
) -> None:
    started, release = asyncio.Event(), asyncio.Event()
    _, video_id = await add_job(env)
    worker = make_worker(env, providers={"mock": blocking_provider(env.storage, started, release)})
    run = asyncio.create_task(worker.run())
    try:
        await asyncio.wait_for(started.wait(), TIMEOUT)
        worker.request_stop()  # grace 0,2 giây → job bị hủy ở giai đoạn provider
        await asyncio.wait_for(run, TIMEOUT)
    finally:
        release.set()
    await settle_background()

    row = await video_row(env, video_id)
    assert (row.status, row.attempt_count, row.active_slot, row.lease_token) == (
        VideoStatus.QUEUED,
        0,
        1,
        None,
    )
    assert videos_dir(env) == []


async def test_second_signal_skips_the_grace_period(tmp_path: Path) -> None:
    env = await make_env(tmp_path, worker_shutdown_grace_seconds=60)
    started, release = asyncio.Event(), asyncio.Event()
    try:
        _, video_id = await add_job(env)
        worker = make_worker(
            env, providers={"mock": blocking_provider(env.storage, started, release)}
        )
        run = asyncio.create_task(worker.run())
        await asyncio.wait_for(started.wait(), TIMEOUT)
        worker.request_stop()
        worker.request_stop()  # tín hiệu thứ hai: không chờ 60 giây
        await asyncio.wait_for(run, 5)
        await settle_background()
        row = await video_row(env, video_id)
    finally:
        release.set()
        await env.engine.dispose()

    assert (row.status, row.attempt_count) == (VideoStatus.QUEUED, 0)


async def test_stop_during_output_stage_does_not_requeue(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, proceed = threading.Event(), threading.Event()
    real_replace = os.replace

    def blocking_replace(*args: Any) -> None:
        entered.set()
        assert proceed.wait(TIMEOUT)
        real_replace(*args)

    monkeypatch.setattr(os, "replace", blocking_replace)
    owner, video_id = await add_job(env)
    worker = make_worker(env)
    run = asyncio.create_task(worker.run())
    try:
        assert await asyncio.to_thread(entered.wait, TIMEOUT)
        worker.request_stop()
        await asyncio.sleep(0.4)  # quá grace 0,2 giây → job bị hủy khi đang đổi chỗ file
    finally:
        proceed.set()
    await asyncio.wait_for(run, TIMEOUT)
    await settle_background()

    row = await video_row(env, video_id)
    assert (row.status, row.attempt_count) == (VideoStatus.PROCESSING, 1)  # để reaper xử lý
    [stored] = videos_dir(env)  # thread đã đổi chỗ xong → giữ file + hạn mức (orphan → P2-8)
    assert await used(env, owner) == stored.stat().st_size


# --------------------------------------------------------------------------- app/worker.py
def test_signal_handlers_cover_sigint_sigterm_and_sigbreak_only_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed: dict[int, Any] = {}
    monkeypatch.setattr(
        signal, "signal", lambda signum, handler: installed.setdefault(signum, handler)
    )
    if hasattr(signal, "SIGBREAK"):
        monkeypatch.delattr(signal, "SIGBREAK")

    install_signal_handlers(asyncio.new_event_loop(), lambda: None)

    assert set(installed) == {signal.SIGINT, signal.SIGTERM}


async def test_signal_handler_schedules_stop_on_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    handlers: dict[int, Any] = {}
    monkeypatch.setattr(
        signal, "signal", lambda signum, handler: handlers.setdefault(signum, handler)
    )
    stopped = asyncio.Event()

    install_signal_handlers(asyncio.get_running_loop(), stopped.set)
    handlers[signal.SIGTERM](signal.SIGTERM, None)  # tín hiệu đến từ thread chính của OS

    await asyncio.wait_for(stopped.wait(), TIMEOUT)


async def test_run_worker_processes_jobs_and_disposes_the_engine(tmp_path: Path) -> None:
    env = await make_env(tmp_path)
    _, video_id = await add_job(env)
    await env.engine.dispose()

    result = await asyncio.wait_for(run_worker(env.settings, exit_when_idle=True), TIMEOUT)

    assert result.engine_disposed is True and result.alive == 0
    check = create_engine(env.settings)
    try:
        async with create_session_factory(check)() as session:
            row = await session.get(Video, video_id)
    finally:
        await check.dispose()
    assert row is not None and row.status == VideoStatus.SUCCEEDED


# --------------------------------------------------------------------------- nhánh lỗi của runner
async def test_unexpected_error_while_storing_leaves_the_job_for_the_reaper(
    env: Env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken_store(*_: object) -> None:
        raise RuntimeError("lưu lỗi (giả lập)")

    monkeypatch.setattr(VideoOutputStore, "store", broken_store)
    _, video_id = await add_job(env)

    await asyncio.wait_for(make_worker(env, exit_when_idle=True).run(), TIMEOUT)

    row = await video_row(env, video_id)
    assert (row.status, row.attempt_count) == (
        VideoStatus.PROCESSING,
        1,
    )  # reaper xử lý khi hết lease
    assert "RuntimeError" in caplog.text


async def test_progress_cas_failure_is_treated_as_a_lost_lease(env: Env) -> None:
    reported = asyncio.Event()

    async def reports_then_waits(job: VideoJob, report: ProgressReporter) -> TempFile:
        async with env.session_factory() as session:  # worker khác đã lấy job
            await session.execute(update(Video).values(lease_token="e" * 32))
            await session.commit()
        await report(10)
        reported.set()
        await asyncio.Event().wait()
        raise AssertionError("không tới đây")

    _, video_id = await add_job(env)
    worker = make_worker(
        env, providers={"mock": FakeProvider(reports_then_waits)}, exit_when_idle=True
    )

    await asyncio.wait_for(worker.run(), TIMEOUT)

    assert reported.is_set()
    row = await video_row(env, video_id)
    assert (row.status, row.lease_token) == (VideoStatus.PROCESSING, "e" * 32)


async def test_failure_after_losing_the_lease_changes_nothing(env: Env) -> None:
    async def lose_then_fail(job: VideoJob, report: ProgressReporter) -> TempFile:
        async with env.session_factory() as session:
            await session.execute(update(Video).values(lease_token="e" * 32))
            await session.commit()
        raise ValueError("bug")

    _, video_id = await add_job(env)
    worker = make_worker(env, providers={"mock": FakeProvider(lose_then_fail)}, exit_when_idle=True)

    await asyncio.wait_for(worker.run(), TIMEOUT)

    row = await video_row(env, video_id)
    assert (row.status, row.error_code) == (
        VideoStatus.PROCESSING,
        None,
    )  # không ghi đè worker khác


async def test_db_error_while_scheduling_a_retry_leaves_the_job_for_the_reaper(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def flaky(*_: object) -> TempFile:
        raise TimeoutError

    async def broken_retry(*_: object) -> None:
        raise OperationalError("UPDATE", {}, Exception("database is locked"))

    monkeypatch.setattr(VideoJobService, "retry_or_fail", broken_retry)
    _, video_id = await add_job(env)

    await asyncio.wait_for(
        make_worker(env, providers={"mock": FakeProvider(flaky)}, exit_when_idle=True).run(),
        TIMEOUT,
    )

    assert (await video_row(env, video_id)).status == VideoStatus.PROCESSING


async def test_slow_requeue_survives_a_second_cancel_and_runs_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = await make_env(tmp_path, shutdown_cancel_grace_seconds=0.1)
    started, release, requeue_release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    requeues: list[int] = []
    real_requeue = VideoJobService.requeue_after_shutdown

    async def slow_requeue(self: VideoJobService, job: VideoJob) -> bool:
        requeues.append(1)
        await asyncio.wait_for(requeue_release.wait(), TIMEOUT)
        return await real_requeue(self, job)

    monkeypatch.setattr(VideoJobService, "requeue_after_shutdown", slow_requeue)
    try:
        _, video_id = await add_job(env)
        worker = make_worker(
            env, providers={"mock": blocking_provider(env.storage, started, release)}
        )
        run = asyncio.create_task(worker.run())
        await asyncio.wait_for(started.wait(), TIMEOUT)
        worker.request_stop()
        await asyncio.wait_for(run, TIMEOUT)  # hủy 2 lần: runner không chờ requeue chậm tới hết
        assert requeues == [1] and len(BACKGROUND_TASKS.pending()) == 1
    finally:
        requeue_release.set()
        release.set()
    await settle_background()
    try:
        row = await video_row(env, video_id)
    finally:
        await env.engine.dispose()

    assert requeues == [1]  # requeue chỉ chạy MỘT lần
    assert (row.status, row.attempt_count) == (VideoStatus.QUEUED, 0)


async def test_db_error_while_requeueing_leaves_the_job_for_the_reaper(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, release = asyncio.Event(), asyncio.Event()

    async def broken_requeue(*_: object) -> bool:
        raise OperationalError("UPDATE", {}, Exception("database is locked"))

    monkeypatch.setattr(VideoJobService, "requeue_after_shutdown", broken_requeue)
    _, video_id = await add_job(env)
    worker = make_worker(env, providers={"mock": blocking_provider(env.storage, started, release)})
    run = asyncio.create_task(worker.run())
    try:
        await asyncio.wait_for(started.wait(), TIMEOUT)
        worker.request_stop()
        await asyncio.wait_for(run, TIMEOUT)
    finally:
        release.set()
    await settle_background()

    assert (await video_row(env, video_id)).status == VideoStatus.PROCESSING
