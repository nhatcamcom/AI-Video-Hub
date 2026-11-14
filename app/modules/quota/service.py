"""Giữ chỗ / hoàn lại dung lượng lưu trữ theo user, nguyên tử kể cả khi nhiều request đồng thời.

Cùng mẫu với `AccountThrottle` (Phase 1): mỗi thay đổi là MỘT câu UPDATE có điều kiện rồi kiểm tra
`rowcount` → điều kiện được database đánh giá trên giá trị mới nhất (PostgreSQL đánh giá lại WHERE
sau khi chờ khóa hàng; SQLite chỉ cho một writer tại một thời điểm).

Mỗi phương thức tự COMMIT một transaction ngắn: phía gọi KHÔNG giữ transaction trong lúc
ghi/đổi chỗ/xóa file. Vì vậy phải dùng session không có thay đổi đang chờ (session riêng cho
thao tác hạn mức).
"""

import logging
from typing import Any, cast

from sqlalchemy import CursorResult, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_now
from app.modules.quota.errors import QuotaUserNotFoundError, StorageQuotaExceededError
from app.modules.quota.models import UserStorageUsage

logger = logging.getLogger(__name__)

# Hai request cùng tạo hàng đầu tiên: bên thua thử lại bằng UPDATE; vài lượt là quá đủ
MAX_RESERVE_ATTEMPTS = 3


def _require_positive(size_bytes: int) -> None:
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes <= 0:
        raise ValueError("size_bytes phải là số nguyên dương")


class StorageQuotaService:
    def __init__(self, db: AsyncSession, quota_bytes: int) -> None:
        self.db = db
        self.quota_bytes = quota_bytes

    async def usage(self, user_id: int) -> int:
        used = await self.db.scalar(
            select(UserStorageUsage.used_bytes).where(UserStorageUsage.user_id == user_id)
        )
        await self.db.commit()  # đóng transaction đọc
        return used or 0

    async def reserve(self, user_id: int, size_bytes: int) -> None:
        """Cộng `size_bytes` (số byte THỰC đã ghi) nếu tổng không vượt hạn mức.

        Raises:
            StorageQuotaExceededError: không đủ chỗ (không thay đổi gì).
            QuotaUserNotFoundError: user không tồn tại.
            ValueError: `size_bytes` không phải số nguyên dương.
        """
        _require_positive(size_bytes)
        if size_bytes > self.quota_bytes:
            raise StorageQuotaExceededError
        for _ in range(MAX_RESERVE_ATTEMPTS):
            if await self._try_increase(user_id, size_bytes):
                await self.db.commit()
                return
            if await self._row_exists(user_id):
                await self.db.rollback()
                raise StorageQuotaExceededError
            if await self._try_create_row(user_id, size_bytes):
                return
            if not await self._row_exists(user_id):
                await self.db.rollback()
                raise QuotaUserNotFoundError(f"không có user_id={user_id}")
        raise RuntimeError("không giữ được chỗ do tranh chấp liên tục")

    async def release(self, user_id: int, size_bytes: int) -> bool:
        """Trừ `size_bytes` sau khi file ĐÃ bị xóa (quyết định 7.7).

        Không bao giờ đưa `used_bytes` về âm hay về 0 để "chữa" sai lệch: nếu số dư không đủ thì
        giữ nguyên dữ liệu, ghi cảnh báo và trả False để bước đối soát xử lý.
        """
        _require_positive(size_bytes)
        released = await self._execute_update(
            update(UserStorageUsage)
            .where(
                UserStorageUsage.user_id == user_id,
                UserStorageUsage.used_bytes >= size_bytes,
            )
            .values(used_bytes=UserStorageUsage.used_bytes - size_bytes, updated_at=utc_now())
        )
        await self.db.commit()
        if not released:
            logger.warning(
                "Hoàn lại %d byte cho user_id=%s vượt số dư đang ghi nhận; cần đối soát",
                size_bytes,
                user_id,
            )
        return released

    async def set_used(self, user_id: int, used_bytes: int) -> None:
        """CHỈ dùng cho đối soát: ghi tổng byte ĐO ĐƯỢC từ các file còn được tham chiếu.

        Không dùng để cấp thêm chỗ hay bỏ qua hạn mức; giá trị có thể vượt hạn mức nếu đó là sự thật
        (khi đó mọi lần giữ chỗ tiếp theo đều bị từ chối).
        """
        if isinstance(used_bytes, bool) or not isinstance(used_bytes, int) or used_bytes < 0:
            raise ValueError("used_bytes phải là số nguyên không âm")
        now = utc_now()
        updated = await self._execute_update(
            update(UserStorageUsage)
            .where(UserStorageUsage.user_id == user_id)
            .values(used_bytes=used_bytes, updated_at=now)
        )
        if not updated:
            await self.db.execute(
                insert(UserStorageUsage).values(
                    user_id=user_id, used_bytes=used_bytes, updated_at=now
                )
            )
        await self.db.commit()
        logger.info("Đối soát dung lượng user_id=%s: %d byte", user_id, used_bytes)

    async def _try_increase(self, user_id: int, size_bytes: int) -> bool:
        return await self._execute_update(
            update(UserStorageUsage)
            .where(
                UserStorageUsage.user_id == user_id,
                UserStorageUsage.used_bytes + size_bytes <= self.quota_bytes,
            )
            .values(used_bytes=UserStorageUsage.used_bytes + size_bytes, updated_at=utc_now())
        )

    async def _row_exists(self, user_id: int) -> bool:
        found = await self.db.scalar(
            select(UserStorageUsage.user_id).where(UserStorageUsage.user_id == user_id)
        )
        return found is not None

    async def _try_create_row(self, user_id: int, size_bytes: int) -> bool:
        # size_bytes <= quota đã được kiểm ở reserve → hàng mới không thể vượt hạn mức
        try:
            await self.db.execute(
                insert(UserStorageUsage).values(
                    user_id=user_id, used_bytes=size_bytes, updated_at=utc_now()
                )
            )
            await self.db.commit()
        except IntegrityError:
            # Request khác vừa tạo hàng (thử lại bằng UPDATE) hoặc user không tồn tại (khóa ngoại)
            await self.db.rollback()
            return False
        return True

    async def _execute_update(self, statement: Any) -> bool:
        result = cast(CursorResult[Any], await self.db.execute(statement))
        return result.rowcount == 1
