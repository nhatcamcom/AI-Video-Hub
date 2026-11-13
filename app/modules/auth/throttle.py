"""Chống brute-force theo tài khoản / thiết bị, lưu trong database.

Vì sao không dùng bộ nhớ in-memory như giới hạn IP:
- Không có "sức chứa" để làm đầy: email giả chỉ thêm hàng vào bảng, không lấy mất chỗ của email
  thật, nên không cần (và không có) chế độ bỏ qua khi đầy.
- Dùng chung mọi worker / máy (cùng database), không mất khi khởi động lại.
- Khóa là HMAC(rule, email) với khóa bí mật: cùng một cách xử lý cho email có thật hay không
  (không lộ tài khoản tồn tại) và database không chứa email thô của người bị dò.

Thuật toán GCRA (một cột): mỗi lượt đẩy `allowed_at_ms` thêm một khoảng `window / max_requests`;
được phép khi giá trị đó chưa vượt quá `now + window`. Cho phép dồn tối đa `max_requests` lượt,
sau đó hồi một lượt mỗi khoảng. Giữ chỗ bằng MỘT câu UPDATE có điều kiện → nguyên tử kể cả khi
nhiều request / worker cùng lúc (PostgreSQL đánh giá lại WHERE sau khi chờ khóa hàng).
"""

import hashlib
import hmac
import math
from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, case, delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rate_limit import RateLimitRule
from app.modules.auth.models import AuthThrottle

MILLISECONDS = 1000
# Mỗi lần thêm hàng mới thì dọn tối đa ngần này hàng đã hồi đầy → bảng chỉ chứa hàng còn hiệu lực
PURGE_BATCH_SIZE = 100
# Hai request cùng chèn một khóa: bên thua chèn thử lại bằng UPDATE; vài lượt là quá đủ
MAX_RESERVE_ATTEMPTS = 3


def _to_ms(moment: datetime) -> int:
    return int(moment.timestamp() * MILLISECONDS)


def _interval_ms(rule: RateLimitRule) -> int:
    return rule.window_seconds * MILLISECONDS // rule.max_requests


def _tolerance_ms(rule: RateLimitRule) -> int:
    return rule.window_seconds * MILLISECONDS - _interval_ms(rule)


class AccountThrottle:
    def __init__(self, db: AsyncSession, key: bytes) -> None:
        self.db = db
        self._key = key

    def key_for(self, rule: RateLimitRule, subject: str) -> str:
        message = f"{rule.name}\x00{subject}".encode()
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()

    async def reserve(
        self, rules: Sequence[RateLimitRule], subject: str, now: datetime
    ) -> int | None:
        """Giữ một lượt ở mọi rule (commit ngay). None = được phép, số = giây phải chờ.

        Rule bị chặn sau khi rule trước đã giữ chỗ thì lượt đó vẫn bị tính ở rule trước: chỉ làm
        giới hạn chặt hơn, không bao giờ lỏng hơn.
        """
        for rule in rules:
            retry_after = await self._reserve_one(rule, subject, _to_ms(now))
            if retry_after is not None:
                return retry_after
        return None

    async def clear(self, rules: Sequence[RateLimitRule], subject: str) -> None:
        """Xóa bộ đếm (đăng nhập thành công / đặt lại mật khẩu). Người gọi commit."""
        keys = [self.key_for(rule, subject) for rule in rules]
        await self.db.execute(delete(AuthThrottle).where(AuthThrottle.key_hash.in_(keys)))

    async def purge_recovered(self, now: datetime) -> int:
        """Xóa mọi hàng đã hồi đầy (tương đương chưa từng có). Người gọi commit."""
        result = cast(
            CursorResult[Any],
            await self.db.execute(
                delete(AuthThrottle).where(AuthThrottle.allowed_at_ms <= _to_ms(now))
            ),
        )
        return result.rowcount

    async def _reserve_one(self, rule: RateLimitRule, subject: str, now_ms: int) -> int | None:
        key = self.key_for(rule, subject)
        for _ in range(MAX_RESERVE_ATTEMPTS):
            if await self._try_advance(key, rule, now_ms):
                await self.db.commit()
                return None
            allowed_at_ms = await self._allowed_at(key)
            if allowed_at_ms is not None:
                await self.db.commit()  # kết thúc transaction đọc
                wait_ms = allowed_at_ms - _tolerance_ms(rule) - now_ms
                return max(1, math.ceil(wait_ms / MILLISECONDS))
            if await self._try_insert(key, now_ms + _interval_ms(rule), now_ms):
                return None
        raise RuntimeError("không giữ được lượt rate limit do tranh chấp liên tục")

    async def _try_advance(self, key: str, rule: RateLimitRule, now_ms: int) -> bool:
        current = AuthThrottle.allowed_at_ms
        # max(allowed_at, now) viết bằng CASE để chạy giống nhau trên SQLite và PostgreSQL
        later_of_now_and_current = case((current > now_ms, current), else_=now_ms)
        result = cast(
            CursorResult[Any],
            await self.db.execute(
                update(AuthThrottle)
                .where(AuthThrottle.key_hash == key, current <= now_ms + _tolerance_ms(rule))
                .values(allowed_at_ms=later_of_now_and_current + _interval_ms(rule))
            ),
        )
        return result.rowcount == 1

    async def _allowed_at(self, key: str) -> int | None:
        return (
            await self.db.execute(
                select(AuthThrottle.allowed_at_ms).where(AuthThrottle.key_hash == key)
            )
        ).scalar_one_or_none()

    async def _try_insert(self, key: str, allowed_at_ms: int, now_ms: int) -> bool:
        try:
            await self.db.execute(
                insert(AuthThrottle).values(key_hash=key, allowed_at_ms=allowed_at_ms)
            )
            await self._purge_batch(now_ms)
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()  # request khác vừa chèn cùng khóa → thử lại bằng UPDATE
            return False
        return True

    async def _purge_batch(self, now_ms: int) -> None:
        recovered = (
            select(AuthThrottle.key_hash)
            .where(AuthThrottle.allowed_at_ms <= now_ms)
            .order_by(AuthThrottle.allowed_at_ms)
            .limit(PURGE_BATCH_SIZE)
        )
        await self.db.execute(delete(AuthThrottle).where(AuthThrottle.key_hash.in_(recovered)))
