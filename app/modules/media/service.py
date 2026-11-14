"""Nghiệp vụ ảnh đầu vào (P2-4).

Luồng upload (không giữ transaction trong lúc đọc / giải mã / ghi file):
1. `reserve_upload_rate_limit`: giữ lượt rate limit theo user (database). Router gọi bước này
   TRƯỚC khi parse body multipart, nên request bị chặn không làm server đọc byte body nào.
2. `upload_image`: ghi upload vào `tmp/`, đếm byte THẬT, dừng ngay khi vượt
   `image_upload_max_bytes`.
3. Magic bytes → kiểm header (định dạng, ảnh động, giới hạn pixel) — trước khi xin slot xử lý.
4. Giải mã + mã hóa lại trong `ImageProcessingGate` (tối đa 2 ảnh cùng lúc).
5. Ghi bản đã làm sạch vào `tmp/` → giữ chỗ hạn mức đúng số byte đó → đổi chỗ sang `uploads/`
   → INSERT (flush) → COMMIT, trên một session RIÊNG của thao tác lưu (không phải session của
   request).

Hoàn tác khi lỗi (P2-4.1, hợp đồng quota ở kế hoạch 4.2). Bất biến: CHỈ hoàn hạn mức khi chắc chắn
file đích không còn và không có bản ghi đã commit tham chiếu tới nó; không xác định được → giữ file
+ hạn mức, log khóa tương đối kèm "cần đối soát". Cụ thể:
- giữ chỗ bị hủy / lỗi lạ → không hoàn (UPDATE có thể đã commit);
- đổi chỗ: chỉ hoàn khi `CommitOutcome.NOT_COMMITTED`; đã đổi chỗ hoặc không xác định → giữ;
- lỗi TRƯỚC COMMIT → rollback, xóa file, chỉ hoàn khi xác nhận file đích đã không còn;
- lỗi / hủy KHI COMMIT → không xóa, không hoàn; lỗi thường thì kiểm tra lại bằng session mới
  (đúng `owner_id` + `storage_key`): thấy bản ghi → coi là thành công (201).
Việc dọn dẹp chạy trong MỘT task riêng, được giữ tham chiếu và sở hữu session lưu (tự đóng khi
xong): bị hủy lần nữa trong lúc chờ thì task vẫn chạy tiếp, không tạo lại, không hoàn hai lần,
không đóng session khi task còn dùng. `CancelledError` luôn được ném lại.
File tạm (bản gốc và bản đã làm sạch) luôn bị xóa, kể cả khi lỗi.
"""

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile

from app.core.background_tasks import BACKGROUND_TASKS
from app.core.config import Settings
from app.core.errors import NotFoundError, PayloadTooLargeError, RateLimitError
from app.core.storage import (
    CommitAttempt,
    CommitOutcome,
    DeleteOutcome,
    FileState,
    InvalidStorageKeyError,
    LocalStorage,
    StorageKind,
    TempFile,
)
from app.core.time import utc_now
from app.modules.auth.throttle import AccountThrottle
from app.modules.media.errors import UnsupportedImageTypeError
from app.modules.media.image_processing import (
    FORMAT_SPECS,
    SNIFF_SIZE_BYTES,
    ImageFormat,
    ImageProcessingGate,
    ProcessedImage,
    detect_image_format,
    inspect_image,
    process_image,
)
from app.modules.media.models import MediaAsset
from app.modules.media.rate_limits import MEDIA_UPLOAD_USER_RULES, media_user_subject
from app.modules.quota.errors import StorageQuotaExceededError
from app.modules.quota.service import StorageQuotaService

logger = logging.getLogger(__name__)

UPLOAD_READ_CHUNK_BYTES = 64 * 1024
IMAGE_RESOURCE_NAME = "Ảnh"
RECONCILE_LOG = "Lưu ảnh: %s (khóa %s, user_id=%s); giữ nguyên file và hạn mức, cần đối soát"

SessionFactory = Callable[[], AsyncSession]


class _Stage(StrEnum):
    RESERVE = "reserve"
    MOVE = "move"
    INSERT = "insert"
    COMMIT = "commit"


@dataclass(frozen=True)
class _CleanupPlan:
    rollback: bool = False
    delete_file: bool = False  # xóa file đích; chỉ hoàn hạn mức khi xác nhận đã không còn
    release_reserved: bool = False  # hoàn hạn mức mà không cần xóa (file chưa từng tới đích)
    reconcile_reason: str | None = None


# Task dọn dẹp có thể sống lâu hơn request (bị hủy lần hai): registry dùng chung của tiến trình
# giữ tham chiếu mạnh tới khi xong, và `lifespan` chờ chúng trước khi dispose engine (P2-7, W8)
CLEANUP_TASK_NAME = "media-cleanup"


def pending_cleanups() -> tuple[asyncio.Task[Any], ...]:
    """Các task dọn dẹp ẢNH chưa xong (lấy từ registry dùng chung của tiến trình)."""
    return tuple(
        task for task in BACKGROUND_TASKS.pending() if task.get_name() == CLEANUP_TASK_NAME
    )


def _cleanup_plan(stage: _Stage, error: BaseException, attempt: CommitAttempt) -> _CleanupPlan:
    if stage is _Stage.RESERVE:
        if isinstance(error, StorageQuotaExceededError):
            return _CleanupPlan()  # chắc chắn không giữ được chỗ
        return _CleanupPlan(reconcile_reason="giữ chỗ hạn mức bị gián đoạn, kết quả không xác định")
    if stage is _Stage.MOVE:
        if attempt.outcome is CommitOutcome.NOT_COMMITTED:
            return _CleanupPlan(release_reserved=True)
        return _CleanupPlan(reconcile_reason=f"đổi chỗ file: {attempt.outcome}, chưa có bản ghi")
    if stage is _Stage.INSERT:
        return _CleanupPlan(rollback=True, delete_file=True)
    if isinstance(error, asyncio.CancelledError):
        return _CleanupPlan(rollback=True, reconcile_reason="COMMIT bị hủy, kết quả không xác định")
    return _CleanupPlan(rollback=True)  # lỗi COMMIT thường: kiểm tra lại bằng session mới


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def _limited_chunks(upload: UploadFile, max_bytes: int) -> AsyncIterator[bytes]:
    """Đọc upload theo khối; vượt `max_bytes` (đếm byte thật) → 413 ngay, không đọc tiếp."""
    total = 0
    while chunk := await upload.read(UPLOAD_READ_CHUNK_BYTES):
        total += len(chunk)
        if total > max_bytes:
            raise PayloadTooLargeError
        yield chunk


async def _single_chunk(data: bytes) -> AsyncIterator[bytes]:
    yield data


def _read_head(path: Path) -> bytes:
    with path.open("rb") as handle:
        return handle.read(SNIFF_SIZE_BYTES)


class MediaService:
    def __init__(
        self,
        db: AsyncSession,
        storage: LocalStorage,
        gate: ImageProcessingGate,
        settings: Settings,
        session_factory: SessionFactory,
    ) -> None:
        self.db = db
        self.storage = storage
        self.gate = gate
        self.settings = settings
        # Session riêng cho thao tác lưu ảnh và cho bước kiểm tra lại sau COMMIT không xác định
        self.session_factory = session_factory
        self.throttle = AccountThrottle(db, settings.throttle_key)

    # ----------------------------------------------------------------------- upload
    async def reserve_upload_rate_limit(self, owner_id: int) -> None:
        """Tính một lượt upload cho user; hết lượt → 429. Gọi trước khi đọc body request."""
        retry_after = await self.throttle.reserve(
            MEDIA_UPLOAD_USER_RULES, media_user_subject(owner_id), utc_now()
        )
        if retry_after is not None:
            raise RateLimitError(retry_after_seconds=retry_after)

    async def upload_image(self, owner_id: int, upload: UploadFile) -> MediaAsset:
        """Kiểm tra, làm sạch và lưu ảnh. Phía gọi đã giữ lượt `reserve_upload_rate_limit`."""
        original = await self.storage.write_temp_file(
            _limited_chunks(upload, self.settings.image_upload_max_bytes)
        )
        try:
            image_format, processed = await self._clean_image(original)
        finally:
            await self.storage.discard_temp_file(original)
        return await self._store_image(owner_id, image_format, processed)

    async def _clean_image(self, original: TempFile) -> tuple[ImageFormat, ProcessedImage]:
        image_format = detect_image_format(await asyncio.to_thread(_read_head, original.path))
        if image_format is None:
            raise UnsupportedImageTypeError
        # Chỉ đọc header: ảnh động / quá nhiều pixel bị từ chối mà không tốn slot xử lý
        await asyncio.to_thread(inspect_image, original.path, image_format)
        processed = await self.gate.run(
            process_image, original.path, image_format, self.settings.image_upload_max_bytes
        )
        return image_format, processed

    async def _store_image(
        self, owner_id: int, image_format: ImageFormat, processed: ProcessedImage
    ) -> MediaAsset:
        spec = FORMAT_SPECS[image_format]
        # Tính trước khi giữ chỗ: không còn khoảng hở "file đã ở đích, chưa INSERT" vì việc phụ
        sha256 = await asyncio.to_thread(_sha256_hex, processed.data)
        cleaned = await self.storage.write_temp_file(_single_chunk(processed.data))
        key = self.storage.new_key(StorageKind.UPLOADS, spec.extension)
        asset = MediaAsset(
            owner_id=owner_id,
            storage_key=key,
            content_type=spec.content_type,
            size_bytes=cleaned.size_bytes,
            width=processed.width,
            height=processed.height,
            sha256=sha256,
        )
        try:
            return await self._persist(asset, cleaned)
        finally:
            # Sau khi đổi chỗ thành công file tạm không còn → xóa là no-op
            await self.storage.discard_temp_file(cleaned)

    async def _persist(self, asset: MediaAsset, cleaned: TempFile) -> MediaAsset:
        """Giữ chỗ → đổi chỗ file → INSERT → COMMIT trên session RIÊNG của thao tác lưu."""
        session = self.session_factory()
        attempt = CommitAttempt()
        stage = _Stage.RESERVE
        try:
            await self._quota(session).reserve(asset.owner_id, cleaned.size_bytes)
            stage = _Stage.MOVE
            await self.storage.commit_temp_file(cleaned, asset.storage_key, attempt)
            stage = _Stage.INSERT
            await self._insert_asset(session, asset)
            stage = _Stage.COMMIT
            await self._commit_asset(session)
        except BaseException as error:
            plan = _cleanup_plan(stage, error, attempt)
            # Từ đây task dọn dẹp sở hữu `session` và tự đóng nó
            await self._run_cleanup(
                session, plan, asset.owner_id, asset.storage_key, asset.size_bytes
            )
            if stage is _Stage.COMMIT and not isinstance(error, asyncio.CancelledError):
                return await self._recover_committed(asset.owner_id, asset.storage_key, error)
            raise
        await session.close()
        return asset

    def _quota(self, session: AsyncSession) -> StorageQuotaService:
        return StorageQuotaService(session, self.settings.storage_quota_bytes_per_user)

    async def _insert_asset(self, session: AsyncSession, asset: MediaAsset) -> None:
        """INSERT nhưng CHƯA commit: lỗi ở đây chắc chắn không để lại bản ghi."""
        session.add(asset)
        await session.flush()

    async def _commit_asset(self, session: AsyncSession) -> None:
        """Lỗi / bị hủy ở đây → kết quả không xác định (COMMIT có thể đã thành công)."""
        await session.commit()

    async def _run_cleanup(
        self, session: AsyncSession, plan: _CleanupPlan, owner_id: int, key: str, size_bytes: int
    ) -> None:
        """Tạo ĐÚNG MỘT task dọn dẹp, giữ tham chiếu, rồi chờ nó.

        `shield` không bảo đảm phía ngoài chờ tới khi task xong: bị hủy lần nữa thì phía ngoài ném
        `CancelledError` ngay, còn task chạy tiếp tới hết và tự đóng session.
        """
        task = BACKGROUND_TASKS.spawn(
            self._cleanup(session, plan, owner_id, key, size_bytes), name=CLEANUP_TASK_NAME
        )
        await asyncio.shield(task)

    async def _cleanup(
        self, session: AsyncSession, plan: _CleanupPlan, owner_id: int, key: str, size_bytes: int
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
                await self._quota(session).release(owner_id, size_bytes)
            if reason is not None:
                logger.error(RECONCILE_LOG, reason, key, owner_id)
        except BaseException as exc:
            # Bước đang làm có thể đã xong một phần → không thử lại, không hoàn hạn mức thêm
            reason = f"dọn dẹp bị gián đoạn ở bước {step} ({type(exc).__name__})"
            logger.error(RECONCILE_LOG, reason, key, owner_id)
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            await session.close()

    async def _delete_and_confirm(self, key: str) -> bool:
        """True CHỈ khi đã xóa (hoặc file đã không còn) VÀ xác nhận lại trên đĩa là không còn."""
        if await self.storage.delete(key) is DeleteOutcome.FAILED:
            return False
        return await self.storage.file_state(key) is FileState.ABSENT

    async def _recover_committed(self, owner_id: int, key: str, error: BaseException) -> MediaAsset:
        """COMMIT báo lỗi: kiểm tra bằng session MỚI. Có đúng bản ghi → COMMIT đã thành công."""
        try:
            found = await self._find_committed_asset(owner_id, key)
        except Exception as lookup_error:
            reason = f"COMMIT lỗi và không kiểm tra lại được ({type(lookup_error).__name__})"
            logger.error(RECONCILE_LOG, reason, key, owner_id)
            raise error from lookup_error
        if found is None:
            logger.error(RECONCILE_LOG, "COMMIT lỗi, không thấy bản ghi", key, owner_id)
            raise error
        logger.warning(
            "COMMIT báo lỗi nhưng bản ghi ảnh %s của user_id=%s đã được lưu", key, owner_id
        )
        return found

    async def _find_committed_asset(self, owner_id: int, key: str) -> MediaAsset | None:
        async with self.session_factory() as session:
            asset: MediaAsset | None = await session.scalar(
                select(MediaAsset).where(
                    MediaAsset.owner_id == owner_id, MediaAsset.storage_key == key
                )
            )
            return asset

    # ----------------------------------------------------------------------- đọc
    async def get_image(self, owner_id: int, image_id: int) -> MediaAsset:
        """Ảnh của CHÍNH user. Ảnh của người khác cũng trả 404 (không lộ ảnh có tồn tại)."""
        asset = await self.db.scalar(
            select(MediaAsset).where(
                MediaAsset.id == image_id,
                MediaAsset.owner_id == owner_id,
                MediaAsset.deleted_at.is_(None),
            )
        )
        if asset is None:
            raise NotFoundError(IMAGE_RESOURCE_NAME, image_id)
        return asset

    async def image_content_path(self, asset: MediaAsset) -> Path:
        """Đường dẫn file đã kiểm tra lại (khóa hợp lệ, không trỏ ra ngoài storage, còn tồn tại)."""
        try:
            path = self.storage.path_for(asset.storage_key)
        except InvalidStorageKeyError as exc:
            logger.error("Khóa lưu trữ không hợp lệ cho media_asset id=%s", asset.id)
            raise NotFoundError(IMAGE_RESOURCE_NAME, asset.id) from exc
        if not await asyncio.to_thread(path.is_file):
            logger.error("Thiếu file của media_asset id=%s", asset.id)
            raise NotFoundError(IMAGE_RESOURCE_NAME, asset.id)
        return path
