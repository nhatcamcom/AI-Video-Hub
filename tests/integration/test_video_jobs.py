"""Chuyển trạng thái job cho worker (P2-7): claim, lease, heartbeat, retry, requeue, reaper.

Đồng hồ được tiêm từ ngoài (không sleep). Claim đồng thời dùng nhiều engine trên cùng file SQLite.
"""

import asyncio
import errno
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError

from app.core.config import Settings
from app.core.database import create_engine, create_session_factory
from app.core.time import utc_now
from app.modules.users.models import User
from app.modules.videos.heartbeat import LeaseHeartbeat
from app.modules.videos.jobs import (
    PROCESSING_FAILED,
    PROCESSING_TIMEOUT,
    RetryDecision,
    VideoJobService,
    is_transient_error,
)
from app.modules.videos.models import Video, VideoStatus
from app.modules.videos.providers.base import ProviderError, VideoJob

pytestmark = pytest.mark.integration

GUARD_SECONDS = 10


class FakeClock:
    def __init__(self) -> None:
        self.now = utc_now()

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def jobs(app: FastAPI, settings: Settings, clock: FakeClock) -> VideoJobService:
    return VideoJobService(app.state.session_factory, settings, clock=clock)


async def add_user(app: FastAPI, email: str = "owner@example.com") -> int:
    async with app.state.session_factory() as session:
        user = User(email=email, password_hash="x", full_name="Owner")
        session.add(user)
        await session.commit()
        return user.id


async def add_video(app: FastAPI, owner_id: int, **overrides: Any) -> int:
    values: dict[str, Any] = {
        "owner_id": owner_id,
        "prompt": "prompt bí mật",
        "aspect_ratio": "16:9",
        "duration_seconds": 5,
        "model_key": "mock",
        "status": VideoStatus.QUEUED,
        "active_slot": 1,
        **overrides,
    }
    async with app.state.session_factory() as session:
        video = Video(**values)
        session.add(video)
        await session.commit()
        return video.id


async def row(app: FastAPI, video_id: int) -> Video:
    async with app.state.session_factory() as session:
        video: Video | None = await session.get(Video, video_id)
        assert video is not None
        return video


async def claimed(jobs: VideoJobService) -> VideoJob:
    job = await jobs.claim_next()
    assert job is not None
    return job


# --------------------------------------------------------------------------- claim
async def test_claim_takes_oldest_queued_job_with_a_fresh_lease(
    app: FastAPI, jobs: VideoJobService, clock: FakeClock, settings: Settings
) -> None:
    owner = await add_user(app)
    first = await add_video(app, owner, created_at=clock.now - timedelta(seconds=5))
    await add_video(app, owner, active_slot=2)

    job = await claimed(jobs)

    assert job.id == first and job.owner_id == owner and job.attempt_count == 1
    assert len(job.lease_token) == 32 and job.prompt == "prompt bí mật"
    stored = await row(app, first)
    assert stored.status == VideoStatus.PROCESSING and stored.lease_token == job.lease_token
    assert stored.lease_expires_at == clock.now + timedelta(seconds=settings.worker_lease_seconds)
    assert stored.started_at == clock.now and stored.attempt_count == 1


async def test_claim_returns_none_when_nothing_is_claimable(
    app: FastAPI, jobs: VideoJobService, clock: FakeClock
) -> None:
    owner = await add_user(app)
    await add_video(app, owner, deleted_at=clock.now)  # đã xóa mềm
    await add_video(app, owner, active_slot=2, next_attempt_at=clock.now + timedelta(seconds=30))
    await add_video(app, owner, status=VideoStatus.FAILED, active_slot=None)

    assert await jobs.claim_next() is None
    clock.advance(31)
    assert (await claimed(jobs)).attempt_count == 1  # hết thời gian chờ backoff → claim được


async def test_claimed_job_is_never_claimed_twice(app: FastAPI, jobs: VideoJobService) -> None:
    owner = await add_user(app)
    first = await add_video(app, owner)
    second = await add_video(app, owner, active_slot=2)

    ids = [(await claimed(jobs)).id, (await claimed(jobs)).id]

    assert ids == [first, second]
    assert await jobs.claim_next() is None


async def test_stale_candidate_is_rejected_by_the_status_guard(
    app: FastAPI, jobs: VideoJobService, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = await add_user(app)
    video_id = await add_video(app, owner)
    await claimed(jobs)
    stale = select(Video.id).where(Video.id == video_id).limit(1)  # ứng viên đã bị worker khác lấy

    monkeypatch.setattr(VideoJobService, "next_queued_query", lambda self, now: stale)

    assert await jobs.claim_next() is None
    assert (await row(app, video_id)).attempt_count == 1  # claim thất bại KHÔNG tăng lượt thử


async def test_concurrent_claimers_never_share_a_job(
    app: FastAPI, settings: Settings, clock: FakeClock
) -> None:
    owners = [await add_user(app, f"u{index}@example.com") for index in range(3)]
    expected = {
        await add_video(app, owner, active_slot=slot) for owner in owners for slot in (1, 2)
    }
    engines = [create_engine(settings) for _ in range(4)]
    services = [VideoJobService(create_session_factory(e), settings, clock=clock) for e in engines]

    async def drain_queue(service: VideoJobService) -> list[int]:
        taken: list[int] = []
        while (job := await service.claim_next()) is not None:
            taken.append(job.id)
        return taken

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(drain_queue(service) for service in services)), GUARD_SECONDS
        )
    finally:
        for engine in engines:
            await engine.dispose()

    taken = [video_id for result in results for video_id in result]
    assert sorted(taken) == sorted(expected)  # mỗi job đúng một lần


async def test_many_workers_racing_for_one_job_only_one_wins(
    app: FastAPI, settings: Settings, clock: FakeClock
) -> None:
    owner = await add_user(app)
    await add_video(app, owner)
    engines = [create_engine(settings) for _ in range(6)]
    services = [VideoJobService(create_session_factory(e), settings, clock=clock) for e in engines]
    barrier = asyncio.Barrier(len(services))

    async def race(service: VideoJobService) -> VideoJob | None:
        await barrier.wait()
        return await service.claim_next()

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(race(s) for s in services)), GUARD_SECONDS
        )
    finally:
        for engine in engines:
            await engine.dispose()

    assert sum(result is not None for result in results) == 1


def test_postgres_claim_locks_with_skip_locked(jobs: VideoJobService) -> None:
    query = jobs.next_queued_query(utc_now()).with_for_update(skip_locked=True)

    sql = str(query.compile(dialect=postgresql.dialect()))  # type: ignore[no-untyped-call]

    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "ORDER BY videos.created_at, videos.id" in sql and "LIMIT" in sql


# --------------------------------------------------------------------------- lease / tiến độ
async def test_renew_lease_requires_the_current_token(
    app: FastAPI, jobs: VideoJobService, clock: FakeClock, settings: Settings
) -> None:
    owner = await add_user(app)
    video_id = await add_video(app, owner)
    job = await claimed(jobs)
    clock.advance(20)

    assert await jobs.renew_lease(job) is True
    expected = clock.now + timedelta(seconds=settings.worker_lease_seconds)
    assert (await row(app, video_id)).lease_expires_at == expected
    impostor = VideoJob(**{**job.__dict__, "lease_token": "f" * 32})
    assert await jobs.renew_lease(impostor) is False


async def test_progress_updates_also_extend_the_lease(
    app: FastAPI, jobs: VideoJobService, clock: FakeClock
) -> None:
    owner = await add_user(app)
    video_id = await add_video(app, owner)
    job = await claimed(jobs)
    clock.advance(5)

    assert await jobs.report_progress(job, 45) is True

    stored = await row(app, video_id)
    assert stored.progress == 45 and stored.lease_expires_at is not None
    assert stored.lease_expires_at > clock.now


async def test_fail_finishes_the_job_and_frees_the_slot(
    app: FastAPI, jobs: VideoJobService, clock: FakeClock
) -> None:
    owner = await add_user(app)
    video_id = await add_video(app, owner)
    job = await claimed(jobs)

    assert await jobs.fail(job, "mock_sample_missing") is True

    stored = await row(app, video_id)
    assert (stored.status, stored.error_code, stored.active_slot) == (
        VideoStatus.FAILED,
        "mock_sample_missing",
        None,
    )
    assert stored.lease_token is None and stored.finished_at == clock.now
    assert await jobs.fail(job, "again") is False  # chỉ kết thúc được một lần


async def test_complete_statement_records_output_only_for_the_lease_owner(
    app: FastAPI, jobs: VideoJobService, clock: FakeClock
) -> None:
    owner = await add_user(app)
    video_id = await add_video(app, owner)
    job = await claimed(jobs)
    key = "videos/" + "a" * 32 + ".mp4"
    impostor = VideoJob(**{**job.__dict__, "lease_token": "f" * 32})

    async with app.state.session_factory() as session:
        lost = await session.execute(jobs.complete_statement(impostor, key, 123))
        done = await session.execute(jobs.complete_statement(job, key, 123))
        await session.commit()

    assert (lost.rowcount, done.rowcount) == (0, 1)
    stored = await row(app, video_id)
    assert (stored.status, stored.progress, stored.active_slot) == (
        VideoStatus.SUCCEEDED,
        100,
        None,
    )
    assert (stored.output_storage_key, stored.output_size_bytes) == (key, 123)
    assert stored.lease_token is None and stored.finished_at == clock.now
    assert await jobs.find_stored(job.id, key) is True
    assert await jobs.find_stored(job.id, "videos/" + "b" * 32 + ".mp4") is False


# --------------------------------------------------------------------------- retry
async def test_retry_requeues_with_backoff_while_attempts_remain(
    app: FastAPI, jobs: VideoJobService, clock: FakeClock, settings: Settings
) -> None:
    owner = await add_user(app)
    video_id = await add_video(app, owner)
    job = await claimed(jobs)

    assert await jobs.retry_or_fail(job) is RetryDecision.REQUEUED

    stored = await row(app, video_id)
    assert (stored.status, stored.active_slot, stored.attempt_count) == (VideoStatus.QUEUED, 1, 1)
    backoff = timedelta(seconds=settings.worker_retry_backoff_seconds * job.attempt_count)
    assert stored.next_attempt_at == clock.now + backoff and stored.lease_token is None


async def test_retry_fails_the_job_when_attempts_are_exhausted(
    app: FastAPI, jobs: VideoJobService, clock: FakeClock, settings: Settings
) -> None:
    owner = await add_user(app)
    video_id = await add_video(app, owner, attempt_count=settings.worker_max_attempts - 1)
    job = await claimed(jobs)
    assert job.attempt_count == settings.worker_max_attempts

    assert await jobs.retry_or_fail(job) is RetryDecision.FAILED

    stored = await row(app, video_id)
    assert (stored.status, stored.error_code, stored.active_slot) == (
        VideoStatus.FAILED,
        PROCESSING_FAILED,
        None,
    )


async def test_retry_after_losing_the_lease_changes_nothing(
    app: FastAPI, jobs: VideoJobService
) -> None:
    owner = await add_user(app)
    video_id = await add_video(app, owner)
    job = await claimed(jobs)
    impostor = VideoJob(**{**job.__dict__, "lease_token": "f" * 32})

    assert await jobs.retry_or_fail(impostor) is RetryDecision.LEASE_LOST
    assert (await row(app, video_id)).status == VideoStatus.PROCESSING


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (OperationalError("UPDATE", {}, Exception("database is locked")), True),
        (TimeoutError(), True),
        (OSError(errno.EIO, "I/O"), True),
        (OSError(errno.EBUSY, "busy"), True),
        (OSError(errno.EAGAIN, "again"), True),
        (PermissionError(errno.EACCES, "denied"), False),
        (FileNotFoundError(errno.ENOENT, "missing"), False),
        (ProviderError("mock_sample_missing"), False),
        (ProviderError("transient", retryable=True), True),
        (ValueError("bug"), False),
        (KeyError("bug"), False),
    ],
)
def test_only_transient_errors_are_retryable(error: BaseException, expected: bool) -> None:
    assert is_transient_error(error) is expected


# --------------------------------------------------------------------------- requeue khi dừng (W12)
async def test_controlled_shutdown_requeue_does_not_count_the_attempt(
    app: FastAPI, jobs: VideoJobService
) -> None:
    owner = await add_user(app)
    video_id = await add_video(app, owner)
    job = await claimed(jobs)

    assert await jobs.requeue_after_shutdown(job) is True

    stored = await row(app, video_id)
    assert (stored.status, stored.attempt_count, stored.active_slot) == (VideoStatus.QUEUED, 0, 1)
    assert stored.lease_token is None and stored.next_attempt_at is None


async def test_shutdown_requeue_refuses_wrong_token_or_existing_output(
    app: FastAPI, jobs: VideoJobService
) -> None:
    owner = await add_user(app)
    video_id = await add_video(app, owner)
    job = await claimed(jobs)
    impostor = VideoJob(**{**job.__dict__, "lease_token": "f" * 32})
    assert await jobs.requeue_after_shutdown(impostor) is False

    async with app.state.session_factory() as session:
        await session.execute(
            update(Video)
            .where(Video.id == video_id)
            .values(output_storage_key="videos/" + "c" * 32 + ".mp4", output_size_bytes=9)
        )
        await session.commit()

    assert await jobs.requeue_after_shutdown(job) is False
    assert (await row(app, video_id)).attempt_count == 1


async def test_shutdown_requeue_never_makes_attempts_negative(
    app: FastAPI, jobs: VideoJobService
) -> None:
    owner = await add_user(app)
    video_id = await add_video(app, owner)
    job = await claimed(jobs)
    async with app.state.session_factory() as session:
        await session.execute(update(Video).where(Video.id == video_id).values(attempt_count=0))
        await session.commit()

    assert await jobs.requeue_after_shutdown(job) is False


# --------------------------------------------------------------------------- reaper
async def test_reaper_requeues_or_fails_expired_jobs_only(
    app: FastAPI, jobs: VideoJobService, clock: FakeClock, settings: Settings
) -> None:
    owners = [await add_user(app, f"r{index}@example.com") for index in range(4)]
    retryable = await add_video(app, owners[0])
    exhausted = await add_video(app, owners[1], attempt_count=settings.worker_max_attempts - 1)
    await claimed(jobs)
    await claimed(jobs)
    clock.advance(settings.worker_lease_seconds - 1)
    fresh = await add_video(app, owners[2])
    await claimed(jobs)  # lease còn hạn sau khi đồng hồ chạy tiếp
    done = await add_video(app, owners[3], status=VideoStatus.SUCCEEDED, active_slot=None)
    clock.advance(2)

    result = await jobs.reap_expired()

    assert (result.requeued, result.failed) == (1, 1)
    requeued, failed = await row(app, retryable), await row(app, exhausted)
    assert (requeued.status, requeued.active_slot, requeued.attempt_count) == (
        VideoStatus.QUEUED,
        1,
        1,
    )
    assert requeued.lease_token is None
    assert (failed.status, failed.error_code, failed.active_slot) == (
        VideoStatus.FAILED,
        PROCESSING_TIMEOUT,
        None,
    )
    assert (await row(app, fresh)).status == VideoStatus.PROCESSING
    assert (await row(app, done)).status == VideoStatus.SUCCEEDED


async def test_worker_that_lost_its_lease_cannot_finish_the_job(
    app: FastAPI, jobs: VideoJobService, clock: FakeClock, settings: Settings
) -> None:
    owner = await add_user(app)
    video_id = await add_video(app, owner)
    old = await claimed(jobs)
    clock.advance(settings.worker_lease_seconds + 1)
    await jobs.reap_expired()
    new = await claimed(jobs)  # worker khác nhận lại job (lượt 2)
    key = "videos/" + "d" * 32 + ".mp4"

    async with app.state.session_factory() as session:
        stale = await session.execute(jobs.complete_statement(old, key, 5))
        await session.commit()

    assert stale.rowcount == 0
    assert await jobs.renew_lease(old) is False and await jobs.fail(old, "x") is False
    stored = await row(app, video_id)
    assert (stored.status, stored.lease_token, stored.attempt_count) == (
        VideoStatus.PROCESSING,
        new.lease_token,
        2,
    )


# --------------------------------------------------------------------------- heartbeat
class SteppingSleep:
    """Mỗi lần "ngủ" đẩy đồng hồ đi `step` giây; sau `limit` lần thì chờ mãi (tới khi bị hủy)."""

    def __init__(self, clock: FakeClock, step: float, limit: int) -> None:
        self.clock, self.step, self.limit, self.calls = clock, step, limit, 0
        self.exhausted = asyncio.Event()

    async def __call__(self, _: float) -> None:
        self.calls += 1
        if self.calls > self.limit:
            self.exhausted.set()
            await asyncio.Event().wait()
        self.clock.advance(self.step)
        await asyncio.sleep(0)


def heartbeat_for(
    jobs: VideoJobService, job: VideoJob, clock: FakeClock, sleep: SteppingSleep, lost: list[int]
) -> LeaseHeartbeat:
    return LeaseHeartbeat(
        jobs,
        job,
        interval=10,
        lease_seconds=60,
        on_lost=lambda: lost.append(1),
        clock=clock,
        sleep=sleep,
    )


async def test_heartbeat_keeps_extending_the_lease_while_provider_is_busy(
    app: FastAPI, jobs: VideoJobService, clock: FakeClock
) -> None:
    owner = await add_user(app)
    video_id = await add_video(app, owner)
    job = await claimed(jobs)
    sleep = SteppingSleep(clock, 10, limit=12)  # 120 giây "trôi qua", > lease 60 giây
    lost: list[int] = []
    task = asyncio.create_task(heartbeat_for(jobs, job, clock, sleep, lost).run())

    await asyncio.wait_for(sleep.exhausted.wait(), GUARD_SECONDS)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):  # heartbeat không nuốt CancelledError
        await asyncio.wait_for(task, GUARD_SECONDS)

    stored = await row(app, video_id)
    assert lost == [] and stored.lease_expires_at is not None
    assert stored.lease_expires_at > clock.now  # lease vẫn còn hạn dù đã qua 120 giây


async def test_heartbeat_reports_lost_lease_once_and_stops(
    app: FastAPI, jobs: VideoJobService, clock: FakeClock, settings: Settings
) -> None:
    owner = await add_user(app)
    await add_video(app, owner)
    job = await claimed(jobs)
    async with app.state.session_factory() as session:  # worker khác đã lấy job (token mới)
        await session.execute(update(Video).values(lease_token="e" * 32))
        await session.commit()
    sleep = SteppingSleep(clock, 10, limit=5)
    lost: list[int] = []

    await asyncio.wait_for(heartbeat_for(jobs, job, clock, sleep, lost).run(), GUARD_SECONDS)

    assert lost == [1] and sleep.calls == 1


async def test_heartbeat_survives_transient_db_errors_until_the_lease_would_expire(
    app: FastAPI, jobs: VideoJobService, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = await add_user(app)
    await add_video(app, owner)
    job = await claimed(jobs)
    attempts: list[int] = []

    async def flaky(self: VideoJobService, _: VideoJob) -> bool:
        attempts.append(1)
        raise OperationalError("UPDATE", {}, Exception("database is locked"))

    monkeypatch.setattr(VideoJobService, "renew_lease", flaky)
    sleep = SteppingSleep(clock, 10, limit=20)
    lost: list[int] = []

    await asyncio.wait_for(heartbeat_for(jobs, job, clock, sleep, lost).run(), GUARD_SECONDS)

    assert lost == [1]
    assert len(attempts) == 6  # thử ở 10..60 giây; tới hạn lease mới coi là mất


def test_job_service_uses_injected_clock(jobs: VideoJobService, clock: FakeClock) -> None:
    assert jobs.clock() == clock.now
    assert clock.now.tzinfo is UTC


class FakePostgresSession:
    """Session giả dialect postgresql: kiểm tra nhánh `FOR UPDATE SKIP LOCKED` không cần server."""

    def __init__(self, candidate: int | None) -> None:
        self.candidate = candidate
        self.statements: list[str] = []
        self.bind = type("Bind", (), {"dialect": postgresql.dialect()})()  # type: ignore[no-untyped-call]

    async def scalar(self, statement: Any) -> int | None:
        self.statements.append(str(statement.compile(dialect=self.bind.dialect)))
        return self.candidate


@pytest.mark.parametrize("candidate", [42, None])
async def test_postgres_claim_target_locks_one_row_with_skip_locked(
    jobs: VideoJobService, candidate: int | None
) -> None:
    session = FakePostgresSession(candidate)

    target = await jobs._claim_target(session, utc_now())  # type: ignore[arg-type]

    assert "FOR UPDATE SKIP LOCKED" in session.statements[0]
    assert (target is None) is (candidate is None)
