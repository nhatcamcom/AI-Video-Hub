"""Nghiệp vụ ảnh đầu vào (P2-4).

Luồng upload (không giữ transaction trong lúc đọc / giải mã / ghi file):
1. `reserve_upload_rate_limit`: giữ lượt rate limit theo user (database). Router gọi bước này
   TRƯỚC khi parse body multipart, nên request bị chặn không làm server đọc byte body nào.
2. `upload_image`: ghi upload vào `tmp/`, đếm byte THẬT, dừng ngay khi vượt
   `image_upload_max_bytes`.
3. Magic bytes → kiểm header (định dạng, ảnh động, giới hạn pixel) — trước khi xin slot xử lý.
4. Giải mã + mã hóa lại trong `ImageProcessingGate` (tối đa 2 ảnh cùng lúc).
5. Ghi bản đã làm sạch vào `tmp/` → giữ chỗ hạn mức đúng số byte đó → đổi chỗ sang `uploads/`
   → INSERT. Bước nào lỗi thì hoàn tác đúng các bước trước nó:
   - đổi chỗ lỗi: hoàn hạn mức, xóa file tạm;
   - INSERT lỗi: xóa file đã lưu; CHỈ hoàn hạn mức khi file thật sự không còn (quyết định 7.7),
     nếu không xóa được thì giữ nguyên hạn mức và ghi log để đối soát.
File tạm (bản gốc và bản đã làm sạch) luôn bị xóa, kể cả khi lỗi.
"""

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile

from app.core.config import Settings
from app.core.errors import NotFoundError, PayloadTooLargeError, RateLimitError
from app.core.storage import (
    DeleteOutcome,
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
from app.modules.quota.service import StorageQuotaService

logger = logging.getLogger(__name__)

UPLOAD_READ_CHUNK_BYTES = 64 * 1024
IMAGE_RESOURCE_NAME = "Ảnh"


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
    ) -> None:
        self.db = db
        self.storage = storage
        self.gate = gate
        self.settings = settings
        self.quota = StorageQuotaService(db, settings.storage_quota_bytes_per_user)
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
        cleaned = await self.storage.write_temp_file(_single_chunk(processed.data))
        try:
            await self.quota.reserve(owner_id, cleaned.size_bytes)
            key = self.storage.new_key(StorageKind.UPLOADS, FORMAT_SPECS[image_format].extension)
            try:
                await self.storage.commit_temp_file(cleaned, key)
            except BaseException:
                await self.quota.release(owner_id, cleaned.size_bytes)
                raise
        finally:
            # Sau khi đổi chỗ thành công file tạm không còn → xóa là no-op
            await self.storage.discard_temp_file(cleaned)
        sha256 = await asyncio.to_thread(lambda: hashlib.sha256(processed.data).hexdigest())
        try:
            return await self._insert_asset(
                owner_id=owner_id,
                storage_key=key,
                content_type=FORMAT_SPECS[image_format].content_type,
                size_bytes=cleaned.size_bytes,
                processed=processed,
                sha256=sha256,
            )
        except BaseException:
            await self.db.rollback()
            await self._remove_stored_file(owner_id, key, cleaned.size_bytes)
            raise

    async def _insert_asset(
        self,
        *,
        owner_id: int,
        storage_key: str,
        content_type: str,
        size_bytes: int,
        processed: ProcessedImage,
        sha256: str,
    ) -> MediaAsset:
        asset = MediaAsset(
            owner_id=owner_id,
            storage_key=storage_key,
            content_type=content_type,
            size_bytes=size_bytes,
            width=processed.width,
            height=processed.height,
            sha256=sha256,
        )
        self.db.add(asset)
        await self.db.commit()
        return asset

    async def _remove_stored_file(self, owner_id: int, key: str, size_bytes: int) -> None:
        outcome = await self.storage.delete(key)
        if outcome is DeleteOutcome.FAILED:
            # File vẫn chiếm chỗ trên đĩa → KHÔNG hoàn hạn mức; chỉ log khóa tương đối
            logger.error(
                "Lưu metadata ảnh thất bại và không xóa được file %s của user_id=%s; "
                "giữ nguyên hạn mức, cần đối soát",
                key,
                owner_id,
            )
            return
        await self.quota.release(owner_id, size_bytes)

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
