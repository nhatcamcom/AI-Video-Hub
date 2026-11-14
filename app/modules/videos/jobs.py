"""Chuyển trạng thái job video cho worker (P2-7): claim, lease, retry, requeue, reaper.

Mọi thay đổi là CAS (UPDATE có điều kiện + kiểm tra `rowcount`) trên một session ngắn riêng:
- Claim: SQLite một câu UPDATE có subquery + `AND status='queued'` (một writer tại một thời
  điểm); PostgreSQL `SELECT … FOR UPDATE SKIP LOCKED` rồi UPDATE lease trong CÙNG transaction.
  `attempt_count` chỉ tăng trong câu UPDATE của lần claim thành công.
- Mọi CAS gia hạn / kết thúc job đều có `status='processing' AND lease_token=:token`: worker đã
  mất lease (token bị xóa hoặc đổi) không thể ghi `succeeded` hay đổi trạng thái job nữa.
- Job kết thúc (`succeeded` / `failed`) trả `active_slot` trong CÙNG câu UPDATE.
"""

import errno
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, cast

from sqlalchemy import (
    ColumnElement,
    CursorResult,
    Select,
    Update,
    func,
    literal,
    or_,
    select,
    update,
)
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.database import UTCDateTime
from app.core.time import utc_now
from app.modules.videos.models import Video, VideoStatus
from app.modules.videos.providers.base import ProviderError, VideoJob

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]
LEASE_TOKEN_BYTES = 16  # 128 bit → 32 ký tự hex
POSTGRESQL = "postgresql"
PROGRESS_DONE = 100

# Mã lỗi an toàn (videos.error_code)
PROCESSING_FAILED = "processing_failed"
PROCESSING_TIMEOUT = "processing_timeout"
VIDEO_MODEL_UNAVAILABLE = "video_model_unavailable"
INPUT_IMAGE_UNAVAILABLE = "input_image_unavailable"
STORAGE_QUOTA_EXCEEDED = "storage_quota_exceeded"

TRANSIENT_ERRNOS = frozenset({errno.EAGAIN, errno.EBUSY, errno.EIO})


def is_transient_error(error: BaseException) -> bool:
    """Chỉ lỗi "tạm thời" mới được thử lại (W5); lỗi lập trình / dữ liệu sai → failed ngay."""
    if isinstance(error, ProviderError):
        return error.retryable
    if isinstance(error, OperationalError | TimeoutError):
        return True
    return isinstance(error, OSError) and error.errno in TRANSIENT_ERRNOS


class RetryDecision(StrEnum):
    REQUEUED = "requeued"
    FAILED = "failed"
    LEASE_LOST = "lease_lost"


@dataclass(frozen=True)
class ReapResult:
    requeued: int
    failed: int


def _job_from(video: Video) -> VideoJob:
    if video.lease_token is None:  # không thể xảy ra: CHECK bắt `processing` có lease
        raise RuntimeError("job vừa claim không có lease")
    return VideoJob(
        id=video.id,
        owner_id=video.owner_id,
        model_key=video.model_key,
        aspect_ratio=video.aspect_ratio,
        duration_seconds=video.duration_seconds,
        input_image_id=video.input_image_id,
        attempt_count=video.attempt_count,
        lease_token=video.lease_token,
        prompt=video.prompt,
    )


def _owned(job: VideoJob) -> tuple[ColumnElement[bool], ...]:
    return (
        Video.id == job.id,
        Video.status == VideoStatus.PROCESSING,
        Video.lease_token == job.lease_token,
    )


async def changed_rows(session: AsyncSession, statement: Update) -> int:
    """Số dòng một câu UPDATE có điều kiện (CAS) đã đổi."""
    result = cast(CursorResult[Any], await session.execute(statement))
    return result.rowcount


class VideoJobService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        clock: Clock = utc_now,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.clock = clock

    def _lease_until(self, now: datetime) -> datetime:
        return now + timedelta(seconds=self.settings.worker_lease_seconds)

    def next_queued_query(self, now: datetime) -> Select[int]:
        return (
            select(Video.id)
            .where(
                Video.status == VideoStatus.QUEUED,
                Video.deleted_at.is_(None),
                or_(Video.next_attempt_at.is_(None), Video.next_attempt_at <= now),
            )
            .order_by(Video.created_at, Video.id)
            .limit(1)
        )

    # ----------------------------------------------------------------------- claim
    async def claim_next(self) -> VideoJob | None:
        now, token = self.clock(), secrets.token_hex(LEASE_TOKEN_BYTES)
        async with self.session_factory() as session:
            target = await self._claim_target(session, now)
            if target is None:
                await session.commit()
                return None
            claimed = await changed_rows(
                session,
                update(Video)
                .where(target, Video.status == VideoStatus.QUEUED)
                .values(
                    status=VideoStatus.PROCESSING,
                    lease_token=token,
                    lease_expires_at=self._lease_until(now),
                    attempt_count=Video.attempt_count + 1,
                    started_at=func.coalesce(Video.started_at, literal(now, UTCDateTime())),
                    next_attempt_at=None,
                ),
            )
            await session.commit()
            if claimed != 1:
                return None
            video = await session.scalar(select(Video).where(Video.lease_token == token))
            await session.commit()
        return _job_from(video) if video is not None else None

    async def _claim_target(
        self, session: AsyncSession, now: datetime
    ) -> ColumnElement[bool] | None:
        if session.bind is not None and session.bind.dialect.name == POSTGRESQL:
            # Khóa đúng một dòng, bỏ qua dòng worker khác đang khóa; UPDATE trong cùng transaction
            video_id = await session.scalar(
                self.next_queued_query(now).with_for_update(skip_locked=True)
            )
            return None if video_id is None else Video.id == video_id
        return Video.id == self.next_queued_query(now).scalar_subquery()

    # ----------------------------------------------------------------------- khi đang chạy
    async def renew_lease(self, job: VideoJob) -> bool:
        return await self._update_owned(job, lease_expires_at=self._lease_until(self.clock()))

    async def report_progress(self, job: VideoJob, progress: int) -> bool:
        now = self.clock()
        return await self._update_owned(
            job, progress=progress, lease_expires_at=self._lease_until(now)
        )

    # ----------------------------------------------------------------------- kết thúc
    def complete_statement(self, job: VideoJob, key: str, size_bytes: int) -> Update:
        """CAS hoàn tất; phía gọi tự execute / commit (tách hai bước, xem `output.py`)."""
        return (
            update(Video)
            .where(*_owned(job))
            .values(
                status=VideoStatus.SUCCEEDED,
                progress=PROGRESS_DONE,
                active_slot=None,
                lease_token=None,
                lease_expires_at=None,
                next_attempt_at=None,
                error_code=None,
                finished_at=self.clock(),
                output_storage_key=key,
                output_size_bytes=size_bytes,
            )
        )

    async def fail(self, job: VideoJob, error_code: str) -> bool:
        return await self._update_owned(job, **self._failed_values(error_code))

    async def retry_or_fail(self, job: VideoJob) -> RetryDecision:
        """Lỗi tạm thời: còn lượt → `queued` + backoff; hết lượt → `failed`."""
        if job.attempt_count >= self.settings.worker_max_attempts:
            failed = await self.fail(job, PROCESSING_FAILED)
            return RetryDecision.FAILED if failed else RetryDecision.LEASE_LOST
        backoff = timedelta(seconds=self.settings.worker_retry_backoff_seconds * job.attempt_count)
        requeued = await self._update_owned(
            job, **self._queued_values(next_attempt_at=self.clock() + backoff)
        )
        return RetryDecision.REQUEUED if requeued else RetryDecision.LEASE_LOST

    async def requeue_after_shutdown(self, job: VideoJob) -> bool:
        """Dừng có kiểm soát ở giai đoạn provider: trả job, KHÔNG tính lượt thử (W12)."""
        async with self.session_factory() as session:
            changed = await changed_rows(
                session,
                update(Video)
                .where(
                    *_owned(job),
                    Video.output_storage_key.is_(None),
                    Video.attempt_count > 0,
                )
                .values(attempt_count=Video.attempt_count - 1, **self._queued_values()),
            )
            await session.commit()
        return changed == 1

    async def reap_expired(self) -> ReapResult:
        """Job `processing` hết lease: còn lượt → `queued`; hết lượt → `failed` (trả slot)."""
        now = self.clock()
        expired = (Video.status == VideoStatus.PROCESSING, Video.lease_expires_at < now)
        maximum = self.settings.worker_max_attempts
        async with self.session_factory() as session:
            requeued = await changed_rows(
                session,
                update(Video)
                .where(*expired, Video.attempt_count < maximum)
                .values(**self._queued_values(next_attempt_at=now)),
            )
            failed = await changed_rows(
                session,
                update(Video)
                .where(*expired, Video.attempt_count >= maximum)
                .values(**self._failed_values(PROCESSING_TIMEOUT)),
            )
            await session.commit()
        if requeued or failed:
            logger.warning("Reaper: %d job trả lại hàng đợi, %d job hết lượt", requeued, failed)
        return ReapResult(requeued=requeued, failed=failed)

    async def find_stored(self, job_id: int, key: str) -> bool:
        """Kiểm tra lại bằng session MỚI: COMMIT hoàn tất có thực sự đã lưu kết quả này không."""
        async with self.session_factory() as session:
            found = await session.scalar(
                select(Video.id).where(
                    Video.id == job_id,
                    Video.output_storage_key == key,
                    Video.status == VideoStatus.SUCCEEDED,
                )
            )
        return found is not None

    # ----------------------------------------------------------------------- nội bộ
    def _failed_values(self, error_code: str) -> dict[str, Any]:
        return {
            "status": VideoStatus.FAILED,
            "error_code": error_code,
            "active_slot": None,
            "lease_token": None,  # nosec B105 — xóa lease, không phải mật khẩu
            "lease_expires_at": None,
            "next_attempt_at": None,
            "finished_at": self.clock(),
        }

    @staticmethod
    def _queued_values(next_attempt_at: datetime | None = None) -> dict[str, Any]:
        return {
            "status": VideoStatus.QUEUED,
            "lease_token": None,  # nosec B105 — xóa lease, không phải mật khẩu
            "lease_expires_at": None,
            "next_attempt_at": next_attempt_at,
        }

    async def _update_owned(self, job: VideoJob, **values: Any) -> bool:
        async with self.session_factory() as session:
            changed = await changed_rows(
                session, update(Video).where(*_owned(job)).values(**values)
            )
            await session.commit()
        return changed == 1
