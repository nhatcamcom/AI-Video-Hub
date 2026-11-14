"""Nghiệp vụ tạo / đọc video (P2-6). Không gọi provider, không ghi file, không giữ chỗ quota.

Thứ tự kiểm tra trong `create_video` (lượt rate limit đã được giữ trước đó, kể cả khi bị từ chối):
1. Model đang bật + tổ hợp tỷ lệ / thời lượng (`VideoModelSpec` là nguồn duy nhất).
2. Ảnh đầu vào: model phải nhận ảnh; ảnh phải tồn tại, của chính user, chưa xóa (dùng lại
   `MediaService.get_image`). Ba trường hợp không hợp lệ trả CÙNG một lỗi 422.
3. Kiểm tra sớm hạn mức: đã dùng hết → 409. KHÔNG giữ chỗ (quyết định D1; worker giữ chỗ theo
   byte thật ở P2-7, hợp đồng ở kế hoạch 4.2).
4. Chọn slot trống rồi INSERT. Tranh chấp slot do UNIQUE `(owner_id, active_slot)` phát hiện;
   CHỈ lỗi đó được thử lại. Lỗi FK / CHECK / lỗi lạ được rollback và xử lý riêng, không bao giờ
   bị đổi thành 409.

Không bao giờ ghi prompt vào log.
"""

import logging
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import NotFoundError, RateLimitError
from app.core.time import utc_now
from app.modules.auth.throttle import AccountThrottle
from app.modules.media.service import MediaService
from app.modules.quota.errors import StorageQuotaExceededError
from app.modules.quota.service import StorageQuotaService
from app.modules.videos.catalog import VideoModelCatalog, VideoModelSpec
from app.modules.videos.errors import (
    ActiveVideoLimitError,
    InputImageNotFoundError,
    InputImageNotSupportedError,
    UnsupportedVideoOptionsError,
    VideoModelUnavailableError,
    VideoSlotContentionError,
)
from app.modules.videos.models import ACTIVE_SLOT_CONSTRAINT_NAME, ACTIVE_SLOTS, Video, VideoStatus
from app.modules.videos.rate_limits import VIDEO_CREATE_USER_RULES, video_user_subject
from app.modules.videos.schemas import VideoCreate

logger = logging.getLogger(__name__)

MAX_SLOT_ATTEMPTS = 3
VIDEO_RESOURCE_NAME = "Video"

# SQLite không ghi tên constraint UNIQUE, chỉ ghi danh sách cột → so khớp NGUYÊN VĂN (không so
# chuỗi con, để không nhầm với UNIQUE khác như output_storage_key). Test khóa định dạng này.
SQLITE_UNIQUE_ERROR = "SQLITE_CONSTRAINT_UNIQUE"
SQLITE_FOREIGN_KEY_ERROR = "SQLITE_CONSTRAINT_FOREIGNKEY"
SQLITE_ACTIVE_SLOT_MESSAGE = "UNIQUE constraint failed: videos.owner_id, videos.active_slot"
POSTGRES_UNIQUE_VIOLATION = "23505"
POSTGRES_FOREIGN_KEY_VIOLATION = "23503"


class IntegrityErrorKind(StrEnum):
    ACTIVE_SLOT_CONFLICT = "active_slot_conflict"
    FOREIGN_KEY = "foreign_key"
    OTHER = "other"


def classify_integrity_error(error: IntegrityError) -> IntegrityErrorKind:
    """Thiếu thông tin để chắc chắn → OTHER (không bao giờ thử lại khi không chắc)."""
    driver_error = error.orig
    sqlite_error_name = getattr(driver_error, "sqlite_errorname", None)
    if isinstance(sqlite_error_name, str):
        return _classify_sqlite(sqlite_error_name, str(driver_error))
    sqlstate = getattr(driver_error, "sqlstate", None)
    if isinstance(sqlstate, str):
        return _classify_postgres(sqlstate, _postgres_constraint_name(driver_error))
    return IntegrityErrorKind.OTHER


def _classify_sqlite(error_name: str, message: str) -> IntegrityErrorKind:
    if error_name == SQLITE_UNIQUE_ERROR and message == SQLITE_ACTIVE_SLOT_MESSAGE:
        return IntegrityErrorKind.ACTIVE_SLOT_CONFLICT
    if error_name == SQLITE_FOREIGN_KEY_ERROR:  # SQLite không cho biết FK nào bị vi phạm
        return IntegrityErrorKind.FOREIGN_KEY
    return IntegrityErrorKind.OTHER


def _postgres_constraint_name(driver_error: object) -> str | None:
    # SQLAlchemy asyncpg bọc lỗi driver; lỗi asyncpg gốc nằm ở `.orig` và có `constraint_name`
    name = getattr(getattr(driver_error, "orig", None), "constraint_name", None)
    return name if isinstance(name, str) else None


def _classify_postgres(sqlstate: str, constraint_name: str | None) -> IntegrityErrorKind:
    if sqlstate == POSTGRES_UNIQUE_VIOLATION and constraint_name == ACTIVE_SLOT_CONSTRAINT_NAME:
        return IntegrityErrorKind.ACTIVE_SLOT_CONFLICT
    if sqlstate == POSTGRES_FOREIGN_KEY_VIOLATION:
        return IntegrityErrorKind.FOREIGN_KEY
    return IntegrityErrorKind.OTHER


class VideoService:
    def __init__(
        self,
        db: AsyncSession,
        settings: Settings,
        catalog: VideoModelCatalog,
        media: MediaService,
    ) -> None:
        self.db = db
        self.catalog = catalog
        self.media = media
        self.quota = StorageQuotaService(db, settings.storage_quota_bytes_per_user)
        self.throttle = AccountThrottle(db, settings.throttle_key)

    # ----------------------------------------------------------------------- tạo
    async def reserve_create_rate_limit(self, owner_id: int) -> None:
        """Tính một lượt tạo video cho user (database, tự commit); hết lượt → 429."""
        retry_after = await self.throttle.reserve(
            VIDEO_CREATE_USER_RULES, video_user_subject(owner_id), utc_now()
        )
        if retry_after is not None:
            raise RateLimitError(retry_after_seconds=retry_after)

    async def create_video(self, owner_id: int, request: VideoCreate) -> Video:
        spec = self._require_supported_options(request)
        if request.input_image_id is not None:
            if not spec.supports_input_image:
                raise InputImageNotSupportedError
            await self._require_input_image(owner_id, request.input_image_id)
        if await self.quota.usage(owner_id) >= self.quota.quota_bytes:
            raise StorageQuotaExceededError
        return await self._insert_with_free_slot(owner_id, request)

    def _require_supported_options(self, request: VideoCreate) -> VideoModelSpec:
        spec = self.catalog.get_enabled(request.model_key)
        if spec is None:
            raise VideoModelUnavailableError
        if not spec.supports(request.aspect_ratio, request.duration_seconds):
            raise UnsupportedVideoOptionsError
        return spec

    async def _require_input_image(self, owner_id: int, image_id: int) -> None:
        try:
            await self.media.get_image(owner_id, image_id)
        except NotFoundError as exc:
            raise InputImageNotFoundError from exc

    async def _insert_with_free_slot(self, owner_id: int, request: VideoCreate) -> Video:
        for _ in range(MAX_SLOT_ATTEMPTS):
            slot = await self._first_free_slot(owner_id)
            if slot is None:
                raise ActiveVideoLimitError
            try:
                return await self._insert_video(owner_id, request, slot)
            except IntegrityError as exc:
                # PostgreSQL đánh dấu transaction hỏng sau lỗi → luôn rollback trước khi đọc tiếp
                await self.db.rollback()
                await self._handle_integrity_error(exc, owner_id, request)
        raise VideoSlotContentionError("tranh chấp slot lặp lại dù vẫn báo còn slot trống")

    async def _handle_integrity_error(
        self, error: IntegrityError, owner_id: int, request: VideoCreate
    ) -> None:
        """Trả về bình thường ⇒ được thử lại (CHỈ tranh chấp slot); mọi trường hợp khác ném lỗi."""
        kind = classify_integrity_error(error)
        if kind is IntegrityErrorKind.ACTIVE_SLOT_CONFLICT:
            logger.info("Tranh chấp slot khi tạo video của user_id=%s; thử lại", owner_id)
            return
        if kind is IntegrityErrorKind.FOREIGN_KEY and request.input_image_id is not None:
            # SQLite không cho biết FK nào: đọc lại ảnh — ảnh đã mất → 422, còn ảnh → lỗi FK owner
            await self._require_input_image(owner_id, request.input_image_id)
        raise error

    async def _first_free_slot(self, owner_id: int) -> int | None:
        taken = set(
            (
                await self.db.scalars(
                    select(Video.active_slot).where(
                        Video.owner_id == owner_id, Video.active_slot.is_not(None)
                    )
                )
            ).all()
        )
        return next((slot for slot in ACTIVE_SLOTS if slot not in taken), None)

    async def _insert_video(self, owner_id: int, request: VideoCreate, slot: int) -> Video:
        video = Video(
            owner_id=owner_id,
            prompt=request.prompt,
            input_image_id=request.input_image_id,
            aspect_ratio=request.aspect_ratio.value,
            duration_seconds=request.duration_seconds,
            model_key=request.model_key,
            status=VideoStatus.QUEUED,
            active_slot=slot,
            progress=0,
        )
        self.db.add(video)
        await self.db.commit()
        return video

    # ----------------------------------------------------------------------- đọc
    async def get_video(self, owner_id: int, video_id: int) -> Video:
        """Video của CHÍNH user, chưa xóa. Của người khác cũng 404 (không lộ có tồn tại)."""
        video = await self.db.scalar(
            select(Video).where(
                Video.id == video_id, Video.owner_id == owner_id, Video.deleted_at.is_(None)
            )
        )
        if video is None:
            raise NotFoundError(VIDEO_RESOURCE_NAME, video_id)
        return video
