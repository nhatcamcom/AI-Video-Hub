"""Lưu video đầu ra theo hợp đồng quota ở kế hoạch 4.2 (P2-7, W9).

Giữ chỗ (byte thật) → đổi chỗ file (`CommitAttempt`) → CAS hoàn tất theo lease token (execute) →
COMMIT, trên một session RIÊNG. Bất biến: CHỈ hoàn hạn mức khi chắc chắn file đích không còn và
không có bản ghi đã commit tham chiếu tới nó; không xác định được → giữ file + hạn mức, log khóa
tương đối kèm "cần đối soát" (P2-8 quét orphan / đối soát).

- giữ chỗ bị gián đoạn → không hoàn; hết hạn mức → `QUOTA_EXCEEDED` (phía gọi cho job `failed`);
- đổi chỗ: chỉ hoàn khi `NOT_COMMITTED`; `COMMITTED` / `UNKNOWN` → giữ;
- CAS lỗi khi execute hoặc `rowcount 0` (mất lease) → chắc chắn chưa commit: rollback, xóa file,
  chỉ hoàn khi `FileState.ABSENT`;
- COMMIT lỗi / bị hủy → không xóa, không hoàn; lỗi thường thì kiểm tra lại bằng session MỚI.
Dọn dẹp chạy trong ĐÚNG MỘT task do registry dùng chung theo dõi; task sở hữu session lưu và tự
đóng nó. Không nuốt `CancelledError`. Không bao giờ ghi prompt vào log.
"""

import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.background_tasks import BACKGROUND_TASKS, BackgroundTaskRegistry
from app.core.config import Settings
from app.core.storage import (
    CommitAttempt,
    CommitOutcome,
    DeleteOutcome,
    FileState,
    LocalStorage,
    StorageKind,
    TempFile,
)
from app.modules.quota.errors import StorageQuotaExceededError
from app.modules.quota.service import StorageQuotaService
from app.modules.videos.jobs import VideoJobService, changed_rows
from app.modules.videos.providers.base import VideoJob

logger = logging.getLogger(__name__)

OUTPUT_EXTENSION = ".mp4"
CLEANUP_TASK_NAME = "video-output-cleanup"
RECONCILE_LOG = "Lưu video %s: %s (khóa %s, user_id=%s); giữ nguyên file và hạn mức, cần đối soát"


class StoreOutcome(StrEnum):
    STORED = "stored"
    QUOTA_EXCEEDED = "quota_exceeded"
    LEASE_LOST = "lease_lost"


class LeaseLostError(Exception):
    """CAS hoàn tất đổi 0 dòng: job không còn thuộc lease này (chắc chắn CHƯA commit)."""


class _Stage(StrEnum):
    RESERVE = "reserve"
    MOVE = "move"
    RECORD = "record"
    COMMIT = "commit"


@dataclass(frozen=True)
class _CleanupPlan:
    rollback: bool = False
    delete_file: bool = False  # xóa file đích; chỉ hoàn hạn mức khi xác nhận đã không còn
    release_reserved: bool = False  # file chưa từng tới đích → hoàn hạn mức không cần xóa
    reconcile_reason: str | None = None


def _cleanup_plan(stage: _Stage, error: BaseException, attempt: CommitAttempt) -> _CleanupPlan:
    if stage is _Stage.RESERVE:
        if isinstance(error, StorageQuotaExceededError):
            return _CleanupPlan()
        return _CleanupPlan(reconcile_reason="giữ chỗ hạn mức bị gián đoạn, kết quả không xác định")
    if stage is _Stage.MOVE:
        if attempt.outcome is CommitOutcome.NOT_COMMITTED:
            return _CleanupPlan(release_reserved=True)
        return _CleanupPlan(reconcile_reason=f"đổi chỗ file: {attempt.outcome}, chưa có bản ghi")
    if stage is _Stage.RECORD:
        return _CleanupPlan(rollback=True, delete_file=True)
    if isinstance(error, asyncio.CancelledError):
        return _CleanupPlan(rollback=True, reconcile_reason="COMMIT bị hủy, kết quả không xác định")
    return _CleanupPlan(rollback=True)  # lỗi COMMIT thường: kiểm tra lại bằng session mới


class VideoOutputStore:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        storage: LocalStorage,
        settings: Settings,
        jobs: VideoJobService,
        registry: BackgroundTaskRegistry = BACKGROUND_TASKS,
    ) -> None:
        self.session_factory = session_factory
        self.storage = storage
        self.settings = settings
        self.jobs = jobs
        self.registry = registry

    async def store(self, job: VideoJob, temp: TempFile) -> StoreOutcome:
        key = self.storage.new_key(StorageKind.VIDEOS, OUTPUT_EXTENSION)
        try:
            return await self._persist(job, temp, key)
        finally:
            # Đổi chỗ thành công → file tạm không còn, xóa là no-op
            await self.storage.discard_temp_file(temp)

    async def _persist(self, job: VideoJob, temp: TempFile, key: str) -> StoreOutcome:
        session = self.session_factory()
        attempt = CommitAttempt()
        stage = _Stage.RESERVE
        try:
            await self._quota(session).reserve(job.owner_id, temp.size_bytes)
            stage = _Stage.MOVE
            await self.storage.commit_temp_file(temp, key, attempt)
            stage = _Stage.RECORD
            await self._record(session, job, key, temp.size_bytes)
            stage = _Stage.COMMIT
            await self._commit(session)
        except BaseException as error:
            plan = _cleanup_plan(stage, error, attempt)
            # Từ đây task dọn dẹp sở hữu `session` và tự đóng nó
            await self._run_cleanup(session, plan, job, key, temp.size_bytes)
            if isinstance(error, StorageQuotaExceededError):
                return StoreOutcome.QUOTA_EXCEEDED
            if isinstance(error, LeaseLostError):
                return StoreOutcome.LEASE_LOST
            if stage is _Stage.COMMIT and not isinstance(error, asyncio.CancelledError):
                return await self._recover(job, key, error)
            raise
        await session.close()
        return StoreOutcome.STORED

    def _quota(self, session: AsyncSession) -> StorageQuotaService:
        return StorageQuotaService(session, self.settings.storage_quota_bytes_per_user)

    async def _record(self, session: AsyncSession, job: VideoJob, key: str, size: int) -> None:
        """CAS hoàn tất (CHƯA commit). Đổi 0 dòng → mất lease, chắc chắn chưa ghi gì."""
        if await changed_rows(session, self.jobs.complete_statement(job, key, size)) != 1:
            raise LeaseLostError

    async def _commit(self, session: AsyncSession) -> None:
        """Lỗi / bị hủy ở đây → kết quả không xác định (COMMIT có thể đã thành công)."""
        await session.commit()

    async def _run_cleanup(
        self, session: AsyncSession, plan: _CleanupPlan, job: VideoJob, key: str, size: int
    ) -> None:
        """Tạo ĐÚNG MỘT task dọn dẹp do registry theo dõi, rồi chờ nó.

        `shield` không bảo đảm phía ngoài chờ tới khi task xong: bị hủy lần nữa thì phía ngoài ném
        `CancelledError` ngay, còn task chạy tiếp tới hết và tự đóng session.
        """
        task = self.registry.spawn(
            self._cleanup(session, plan, job, key, size), name=CLEANUP_TASK_NAME
        )
        await asyncio.shield(task)

    async def _cleanup(
        self, session: AsyncSession, plan: _CleanupPlan, job: VideoJob, key: str, size: int
    ) -> None:
        step = "rollback"
        try:
            if plan.rollback:
                await session.rollback()
            release = plan.release_reserved
            reason = plan.reconcile_reason
            if plan.delete_file:
                step = "delete"
                release = await self._delete_and_confirm(key)
                if not release:
                    reason = "không xác nhận được file đích đã bị xóa"
            if release:
                step = "release"
                await self._quota(session).release(job.owner_id, size)
            if reason is not None:
                logger.error(RECONCILE_LOG, job.id, reason, key, job.owner_id)
        except BaseException as exc:
            # Bước đang làm có thể đã xong một phần → không thử lại, không hoàn hạn mức thêm
            reason = f"dọn dẹp bị gián đoạn ở bước {step} ({type(exc).__name__})"
            logger.error(RECONCILE_LOG, job.id, reason, key, job.owner_id)
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            await session.close()

    async def _delete_and_confirm(self, key: str) -> bool:
        if await self.storage.delete(key) is DeleteOutcome.FAILED:
            return False
        return await self.storage.file_state(key) is FileState.ABSENT

    async def _recover(self, job: VideoJob, key: str, error: BaseException) -> StoreOutcome:
        """COMMIT báo lỗi: session MỚI thấy đúng kết quả → COMMIT thực ra đã thành công."""
        try:
            stored = await self.jobs.find_stored(job.id, key)
        except Exception as lookup_error:
            reason = f"COMMIT lỗi và không kiểm tra lại được ({type(lookup_error).__name__})"
            logger.error(RECONCILE_LOG, job.id, reason, key, job.owner_id)
            raise error from lookup_error
        if not stored:
            logger.error(RECONCILE_LOG, job.id, "COMMIT lỗi, không thấy kết quả", key, job.owner_id)
            raise error
        logger.warning("Video %s: COMMIT báo lỗi nhưng kết quả đã được lưu", job.id)
        return StoreOutcome.STORED
