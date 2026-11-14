"""Vòng lặp worker tạo video (P2-7). Điều phối, không chứa SQL (chuyển trạng thái ở `jobs.py`,
lưu đầu ra ở `output.py`).

Mỗi vòng: reaper → claim 1 job → xử lý → lặp. Khi xử lý một job:
1. Heartbeat (task riêng, độc lập với provider) gia hạn lease; mất lease → hủy provider.
2. Giai đoạn provider: kiểm tra model / ảnh đầu vào (W6) → provider tạo file tạm. Bị hủy vì dừng có
   kiểm soát → task requeue do registry theo dõi, KHÔNG tính lượt thử (W12).
3. Giai đoạn output (từ giữ chỗ hạn mức trở đi): KHÔNG requeue; `output.py` xử lý theo hợp đồng
   4.2; kết quả không xác định → job giữ `processing` cho tới khi reaper xử lý.

Dừng (W11): tín hiệu 1 → ngừng claim, chờ job ≤ `worker_shutdown_grace_seconds`; quá hạn hoặc tín
hiệu 2 → hủy job. Không nuốt `CancelledError`. Không bao giờ ghi prompt vào log.
"""

import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable, Mapping

from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.background_tasks import BACKGROUND_TASKS, BackgroundTaskRegistry
from app.core.config import Settings
from app.core.errors import NotFoundError
from app.core.storage import LocalStorage, TempFile
from app.modules.media.image_processing import ImageProcessingGate
from app.modules.media.service import MediaService
from app.modules.videos.catalog import VideoModelCatalog
from app.modules.videos.heartbeat import LeaseHeartbeat
from app.modules.videos.jobs import (
    INPUT_IMAGE_UNAVAILABLE,
    PROCESSING_FAILED,
    STORAGE_QUOTA_EXCEEDED,
    VIDEO_MODEL_UNAVAILABLE,
    VideoJobService,
    is_transient_error,
)
from app.modules.videos.output import StoreOutcome, VideoOutputStore
from app.modules.videos.providers.base import ProviderError, VideoJob, VideoProvider

logger = logging.getLogger(__name__)

ImageAvailable = Callable[[int, int], Awaitable[bool]]
REQUEUE_TASK_NAME = "video-requeue"
JITTER_RATIO = 0.2  # chờ thêm tối đa 20% poll interval để các worker không đồng loạt truy vấn
MILLIS = 1000
# Chỉ để worker đọc ảnh đầu vào qua MediaService.get_image (không xử lý ảnh trong worker)
IMAGE_LOOKUP_GATE_SLOTS = 1


def image_checker(
    session_factory: async_sessionmaker[AsyncSession], storage: LocalStorage, settings: Settings
) -> ImageAvailable:
    """Ảnh đầu vào còn của user và chưa xóa? Dùng lại `MediaService.get_image` (không truy cập
    thẳng model của miền media)."""
    gate = ImageProcessingGate(slots=IMAGE_LOOKUP_GATE_SLOTS)

    async def available(owner_id: int, image_id: int) -> bool:
        async with session_factory() as session:
            media = MediaService(session, storage, gate, settings, session_factory)
            try:
                await media.get_image(owner_id, image_id)
            except NotFoundError:
                return False
        return True

    return available


class VideoWorker:
    def __init__(
        self,
        *,
        settings: Settings,
        jobs: VideoJobService,
        output: VideoOutputStore,
        catalog: VideoModelCatalog,
        providers: Mapping[str, VideoProvider],
        image_available: ImageAvailable,
        registry: BackgroundTaskRegistry = BACKGROUND_TASKS,
        exit_when_idle: bool = False,
    ) -> None:
        self.settings = settings
        self.jobs = jobs
        self.output = output
        self.catalog = catalog
        self.providers = providers
        self.image_available = image_available
        self.registry = registry
        self.exit_when_idle = exit_when_idle
        self._stopping = asyncio.Event()
        self._forced = asyncio.Event()
        self._current: asyncio.Task[None] | None = None

    # ----------------------------------------------------------------------- dừng
    def request_stop(self) -> None:
        """Tín hiệu 1: ngừng claim, chờ job trong thời hạn. Tín hiệu 2: hủy job ngay."""
        if not self._stopping.is_set():
            logger.info("Worker nhận tín hiệu dừng: ngừng nhận job mới")
            self._stopping.set()
            return
        logger.warning("Worker nhận tín hiệu dừng lần nữa: hủy job đang chạy")
        self._forced.set()

    # ----------------------------------------------------------------------- vòng lặp
    async def run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.jobs.reap_expired()
                job = await self.jobs.claim_next()
            except OperationalError as exc:
                logger.warning("Worker: lỗi DB tạm thời khi lấy job (%s)", type(exc).__name__)
                await self._idle()
                continue
            if job is None:
                if self.exit_when_idle:
                    return
                await self._idle()
                continue
            await self._run_job(job)

    async def _idle(self) -> None:
        poll = self.settings.worker_poll_seconds
        jitter = secrets.randbelow(int(poll * JITTER_RATIO * MILLIS) + 1) / MILLIS
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=poll + jitter)
        except TimeoutError:
            return

    async def _run_job(self, job: VideoJob) -> None:
        current = asyncio.create_task(self._process(job), name="video-job")
        self._current = current
        try:
            await self._wait_or_stop(current)
        finally:
            self._current = None
        if not current.cancelled() and current.exception() is not None:
            logger.error("Video %s: lỗi khi xử lý (%s)", job.id, type(current.exception()).__name__)

    async def _wait_or_stop(self, current: asyncio.Task[None]) -> None:
        if await self._first_done(current, self._stopping):
            return
        grace = self.settings.worker_shutdown_grace_seconds
        if await self._first_done(current, self._forced, limit_seconds=grace):
            return
        current.cancel()  # quá hạn hoặc tín hiệu 2 → hủy theo giai đoạn (requeue / dọn dẹp)
        await asyncio.wait({current}, timeout=self.settings.shutdown_cancel_grace_seconds)
        if not current.done():
            current.cancel()  # hủy lần nữa: các bước dọn dẹp vẫn do registry giữ và chạy tiếp
            await asyncio.wait({current})

    @staticmethod
    async def _first_done(
        current: asyncio.Task[None], event: asyncio.Event, limit_seconds: float | None = None
    ) -> bool:
        """Chờ `current` xong hoặc `event` được đặt; True nếu `current` đã xong."""
        waiter = asyncio.create_task(event.wait())
        try:
            await asyncio.wait(
                {current, waiter}, timeout=limit_seconds, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            waiter.cancel()
        return current.done()

    # ----------------------------------------------------------------------- một job
    async def _process(self, job: VideoJob) -> None:
        lease_lost = asyncio.Event()
        provider_task: list[asyncio.Task[TempFile]] = []

        def on_lost() -> None:
            lease_lost.set()
            for task in provider_task:
                task.cancel()

        heartbeat = asyncio.create_task(
            LeaseHeartbeat(
                self.jobs,
                job,
                interval=self.settings.worker_heartbeat_seconds,
                lease_seconds=self.settings.worker_lease_seconds,
                on_lost=on_lost,
                clock=self.jobs.clock,
            ).run(),
            name="video-heartbeat",
        )
        try:
            temp = await self._provider_stage(job, lease_lost, provider_task, on_lost)
            if temp is not None:
                await self._output_stage(job, temp, lease_lost)
        finally:
            heartbeat.cancel()
            await asyncio.wait({heartbeat})

    async def _provider_stage(
        self,
        job: VideoJob,
        lease_lost: asyncio.Event,
        provider_task: list[asyncio.Task[TempFile]],
        on_lost: Callable[[], None],
    ) -> TempFile | None:
        try:
            reason = await self._unavailable_reason(job)
            if reason is not None:
                await self._fail(job, reason)
                return None
            task = asyncio.create_task(
                self.providers[job.model_key].generate(job, self._reporter(job, on_lost))
            )
            provider_task.append(task)
            return await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is None or not current.cancelling():
                return None  # chỉ provider bị hủy vì mất lease: job đã thuộc worker khác / reaper
            if not lease_lost.is_set():
                await self._requeue_after_shutdown(job)
            raise
        except Exception as error:
            await self._handle_provider_error(job, error)
            return None

    async def _output_stage(self, job: VideoJob, temp: TempFile, lease_lost: asyncio.Event) -> None:
        if lease_lost.is_set():  # không giữ chỗ hạn mức cho job đã mất
            await self.output.storage.discard_temp_file(temp)
            return
        try:
            outcome = await self.output.store(job, temp)
        except Exception as error:  # kết quả không xác định → giữ `processing`, reaper xử lý
            logger.error("Video %s: lưu kết quả lỗi (%s)", job.id, type(error).__name__)
            return
        if outcome is StoreOutcome.QUOTA_EXCEEDED:
            await self._fail(job, STORAGE_QUOTA_EXCEEDED)
        elif outcome is StoreOutcome.LEASE_LOST:
            logger.warning("Video %s: mất lease khi lưu kết quả; đã hoàn tác", job.id)
        else:
            logger.info("Video %s: hoàn tất (lượt %d)", job.id, job.attempt_count)

    # ----------------------------------------------------------------------- hỗ trợ
    async def _unavailable_reason(self, job: VideoJob) -> str | None:
        if self.catalog.get_enabled(job.model_key) is None or job.model_key not in self.providers:
            return VIDEO_MODEL_UNAVAILABLE
        image_id = job.input_image_id
        if image_id is not None and not await self.image_available(job.owner_id, image_id):
            return INPUT_IMAGE_UNAVAILABLE
        return None

    def _reporter(
        self, job: VideoJob, on_lost: Callable[[], None]
    ) -> Callable[[int], Awaitable[None]]:
        async def report(progress: int) -> None:
            try:
                still_owned = await self.jobs.report_progress(job, progress)
            except Exception as exc:  # heartbeat vẫn giữ lease; tiến độ chỉ là phụ
                logger.warning("Video %s: không ghi được tiến độ (%s)", job.id, type(exc).__name__)
                return
            if not still_owned:
                on_lost()

        return report

    async def _handle_provider_error(self, job: VideoJob, error: Exception) -> None:
        if isinstance(error, ProviderError) and not error.retryable:
            await self._fail(job, error.code)
        elif is_transient_error(error):
            try:
                decision = await self.jobs.retry_or_fail(job)
            except Exception as exc:
                logger.error("Video %s: không thử lại được (%s)", job.id, type(exc).__name__)
                return
            logger.warning(
                "Video %s: lỗi tạm thời (%s) → %s", job.id, type(error).__name__, decision
            )
        else:
            logger.error("Video %s: lỗi không thử lại (%s)", job.id, type(error).__name__)
            await self._fail(job, PROCESSING_FAILED)

    async def _fail(self, job: VideoJob, code: str) -> None:
        try:
            if not await self.jobs.fail(job, code):
                logger.warning("Video %s: không ghi được failed (mất lease)", job.id)
                return
        except Exception as exc:  # reaper xử lý khi lease hết hạn
            logger.error("Video %s: lỗi khi ghi failed (%s)", job.id, type(exc).__name__)
            return
        logger.info("Video %s: failed (%s)", job.id, code)

    async def _requeue_after_shutdown(self, job: VideoJob) -> None:
        """Task requeue do registry theo dõi; bị hủy lần nữa thì task vẫn chạy tiếp tới hết."""
        task = self.registry.spawn(self._requeue(job), name=REQUEUE_TASK_NAME)
        await asyncio.shield(task)

    async def _requeue(self, job: VideoJob) -> None:
        try:
            requeued = await self.jobs.requeue_after_shutdown(job)
        except Exception as exc:  # reaper xử lý khi lease hết hạn (có tính lượt thử)
            logger.warning("Video %s: không trả được job (%s)", job.id, type(exc).__name__)
            return
        if requeued:
            logger.info("Video %s: dừng có kiểm soát, trả job về hàng đợi", job.id)
