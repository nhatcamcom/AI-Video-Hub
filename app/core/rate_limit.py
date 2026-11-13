"""Giới hạn tần suất truy cập.

`InMemoryRateLimiter` chỉ đúng khi chạy MỘT tiến trình (dev, một worker). Khi chạy nhiều worker
hoặc nhiều máy, thay bằng bản dùng kho chung (Redis/gateway) với cùng interface `RateLimiter`
ở giai đoạn production — router không phải sửa.
"""

import heapq
import itertools
import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import NamedTuple, Protocol

logger = logging.getLogger(__name__)

DEFAULT_MAX_TRACKED_KEYS = 100_000


class WhenFull(StrEnum):
    """Hành vi khi rule đã hết chỗ cho bucket MỚI (bucket đã có luôn được đếm bình thường)."""

    REJECT = "reject"  # fail-closed: rule chính (theo IP / mạng)
    SKIP = "skip"  # bỏ qua rule này cho key mới: rule phụ (theo email), rule IP vẫn bảo vệ


@dataclass(frozen=True)
class RateLimitRule:
    max_requests: int
    window_seconds: int
    name: str = "default"
    when_full: WhenFull = WhenFull.REJECT

    def __post_init__(self) -> None:
        if self.max_requests <= 0 or self.window_seconds <= 0:
            raise ValueError("max_requests và window_seconds phải là số dương")
        if not self.name:
            raise ValueError("rule phải có tên")


class RateLimitCheck(NamedTuple):
    key: str
    rule: RateLimitRule


@dataclass(frozen=True)
class ClientRateLimit:
    """Giới hạn theo IP của một hành động.

    - `ipv4`: theo từng địa chỉ IPv4, fail-closed.
    - `ipv6_client`: theo /64. Một /48 có 65.536 dải /64 nên rule này có pool riêng và khi đầy
      thì bỏ qua /64 MỚI (`WhenFull.SKIP`) — không để dải IPv6 rẻ tiền chiếm chỗ của IPv4.
    - `ipv6_network`: ngân sách chung cho cả /48, fail-closed; luôn áp dụng cho mọi client IPv6.
    """

    ipv4: RateLimitRule
    ipv6_client: RateLimitRule
    ipv6_network: RateLimitRule

    def __post_init__(self) -> None:
        if self.ipv6_client.when_full is not WhenFull.SKIP:
            raise ValueError("rule /64 phải bỏ qua key mới khi đầy; /48 mới là rule chặn")
        if WhenFull.SKIP in (self.ipv4.when_full, self.ipv6_network.when_full):
            raise ValueError("rule IPv4 và /48 phải fail-closed khi đầy")


class RateLimiter(Protocol):
    def hit(self, key: str, rule: RateLimitRule) -> int | None:
        """Ghi nhận một lượt. Trả None nếu được phép, hoặc số giây phải chờ nếu bị chặn."""
        ...

    def hit_all(self, checks: Sequence[RateLimitCheck]) -> int | None:
        """Như `hit` cho nhiều rule cùng lúc: chỉ ghi nhận khi TẤT CẢ cho phép."""
        ...


def allocate_capacity(
    max_tracked_keys: int, shares: Mapping[RateLimitRule, int]
) -> dict[RateLimitRule, int]:
    """Chia tổng sức chứa cho từng rule theo tỉ trọng (làm tròn xuống, mỗi rule ít nhất 1)."""
    total_share = sum(shares.values())
    if total_share <= 0 or any(share <= 0 for share in shares.values()):
        raise ValueError("tỉ trọng sức chứa phải là số dương")
    return {rule: max(1, max_tracked_keys * share // total_share) for rule, share in shares.items()}


BucketKey = tuple[str, RateLimitRule]
SHARED_POOL_NAME = "shared"


@dataclass
class _Bucket:
    """Bộ đếm sliding-window-log của một key trong một rule."""

    timestamps: deque[float] = field(default_factory=deque)

    def expires_at(self, rule: RateLimitRule) -> float:
        # Sau thời điểm này mọi lượt đã ra khỏi cửa sổ → bucket không còn mang thông tin gì
        return self.timestamps[-1] + rule.window_seconds

    def drop_expired(self, now: float, rule: RateLimitRule) -> None:
        cutoff = now - rule.window_seconds
        while self.timestamps and self.timestamps[0] <= cutoff:
            self.timestamps.popleft()


class _Pool:
    """Các bucket dùng chung một sức chứa cố định, có heap hết hạn riêng."""

    def __init__(self, name: str, capacity: int) -> None:
        self.name = name
        self.capacity = capacity
        self.buckets: dict[BucketKey, _Bucket] = {}
        self.expiry_heap: list[tuple[float, int, BucketKey]] = []
        self.is_saturated = False

    def has_room_for(self, new_buckets: int) -> bool:
        return len(self.buckets) + new_buckets <= self.capacity

    def purge_expired(self, now: float, sequence: "itertools.count[int]") -> None:
        while self.expiry_heap and self.expiry_heap[0][0] <= now:
            _, _, bucket_key = heapq.heappop(self.expiry_heap)
            expires_at = self.buckets[bucket_key].expires_at(bucket_key[1])
            if expires_at > now:
                # Bucket đã nhận lượt mới sau lần hẹn trước → hẹn lại theo hạn mới
                self.schedule(expires_at, bucket_key, sequence)
                continue
            del self.buckets[bucket_key]
            self.is_saturated = False

    def schedule(
        self, expires_at: float, bucket_key: BucketKey, sequence: "itertools.count[int]"
    ) -> None:
        heapq.heappush(self.expiry_heap, (expires_at, next(sequence), bucket_key))

    def retry_after(self, now: float) -> int:
        return max(1, math.ceil(self.expiry_heap[0][0] - now))

    def warn_saturated_once(self, rule: RateLimitRule) -> None:
        if self.is_saturated:
            return
        self.is_saturated = True
        outcome = "rejecting" if rule.when_full is WhenFull.REJECT else "not tracking"
        # Chỉ log tên pool và số lượng, không log key (chứa IP/email)
        logger.warning(
            "Rate limiter is full for %r (%d keys); %s new keys until some expire",
            self.name,
            self.capacity,
            outcome,
        )


class _Planned(NamedTuple):
    pool: _Pool
    bucket_key: BucketKey
    bucket: _Bucket | None  # None = bucket mới sẽ được tạo khi mọi rule đều cho phép


class InMemoryRateLimiter:
    """Sliding window log với sức chứa cứng, chia theo rule.

    - Rule có trong `reserved_capacity` có pool riêng với đúng sức chứa đó; mọi rule khác dùng
      chung pool "shared" với phần còn lại. Tổng không bao giờ vượt `max_tracked_keys`, và một
      rule bị làm đầy không lấy được chỗ của rule khác.
    - Mỗi bucket chỉ bị xóa khi CHÍNH NÓ hết hạn, nên rule ngắn không bao giờ xóa được bộ đếm
      của rule dài. Không có LRU: đẩy key cũ ra sẽ cho phép xóa bộ đếm của một tài khoản đang
      bị chặn bằng cách tạo thật nhiều key ngẫu nhiên.
    - Khi pool đầy, key MỚI bị từ chối (`WhenFull.REJECT`) hoặc được bỏ qua ở rule đó
      (`WhenFull.SKIP`); bucket đã có vẫn được đếm chính xác.
    - `hit_all` kiểm tra mọi rule trước rồi mới ghi: lượt bị chặn ở rule này không tiêu ngân
      sách của rule khác, và không tạo bucket nào.
    - Mỗi bucket có đúng một mục trong heap hết hạn của pool → dọn dẹp chỉ tốn chi phí cho các
      bucket thật sự hết hạn, không quét toàn bộ. Thread-safe: mọi thao tác chạy trong một khóa.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        max_tracked_keys: int = DEFAULT_MAX_TRACKED_KEYS,
        reserved_capacity: Mapping[RateLimitRule, int] | None = None,
    ) -> None:
        reserved = dict(reserved_capacity or {})
        if max_tracked_keys <= 0:
            raise ValueError("max_tracked_keys phải là số dương")
        if any(capacity <= 0 for capacity in reserved.values()):
            raise ValueError("sức chứa của mỗi rule phải là số dương")
        shared_capacity = max_tracked_keys - sum(reserved.values())
        if shared_capacity < 0:
            raise ValueError("tổng sức chứa dành riêng vượt quá max_tracked_keys")
        self._clock = clock
        self._pools = {rule: _Pool(rule.name, capacity) for rule, capacity in reserved.items()}
        self._shared_pool = _Pool(SHARED_POOL_NAME, shared_capacity)
        self._all_pools = (*self._pools.values(), self._shared_pool)
        self._sequence = itertools.count()  # phá hòa khi trùng thời điểm, không so sánh key
        self._lock = threading.Lock()

    @property
    def tracked_key_count(self) -> int:
        return sum(len(pool.buckets) for pool in self._all_pools)

    def tracked_key_count_for(self, rule: RateLimitRule) -> int:
        pool = self._pool_for(rule)
        return sum(1 for _, bucket_rule in pool.buckets if bucket_rule == rule)

    def capacity_for(self, rule: RateLimitRule) -> int:
        return self._pool_for(rule).capacity

    def hit(self, key: str, rule: RateLimitRule) -> int | None:
        return self.hit_all([RateLimitCheck(key, rule)])

    def hit_all(self, checks: Sequence[RateLimitCheck]) -> int | None:
        if len(set(checks)) != len(checks):
            raise ValueError("không được kiểm tra cùng một (key, rule) hai lần trong một lượt")
        with self._lock:
            now = self._clock()
            for pool in self._all_pools:
                pool.purge_expired(now, self._sequence)
            planned: list[_Planned] = []
            retry_afters: list[int] = []
            for check in checks:
                outcome = self._evaluate(check, now, planned)
                if isinstance(outcome, int):
                    retry_afters.append(outcome)
                elif outcome is not None:
                    planned.append(outcome)
            if retry_afters:
                return max(retry_afters)  # phải chờ tới khi MỌI rule đều cho phép
            for plan in planned:
                self._record(plan, now)
            return None

    def _pool_for(self, rule: RateLimitRule) -> _Pool:
        return self._pools.get(rule, self._shared_pool)

    def _evaluate(
        self, check: RateLimitCheck, now: float, planned: Sequence[_Planned]
    ) -> _Planned | int | None:
        """Kế hoạch ghi nhận, số giây phải chờ, hoặc None nếu rule này được bỏ qua."""
        bucket_key: BucketKey = (check.key, check.rule)
        pool = self._pool_for(check.rule)
        bucket = pool.buckets.get(bucket_key)
        if bucket is not None:
            bucket.drop_expired(now, check.rule)
            if len(bucket.timestamps) >= check.rule.max_requests:
                return max(1, math.ceil(bucket.timestamps[0] + check.rule.window_seconds - now))
            return _Planned(pool, bucket_key, bucket)

        pending_new = sum(1 for plan in planned if plan.pool is pool and plan.bucket is None)
        if pool.has_room_for(pending_new + 1):
            return _Planned(pool, bucket_key, None)
        pool.warn_saturated_once(check.rule)
        if check.rule.when_full is WhenFull.SKIP:
            return None
        return pool.retry_after(now) if pool.expiry_heap else check.rule.window_seconds

    def _record(self, plan: _Planned, now: float) -> None:
        if plan.bucket is not None:
            plan.bucket.timestamps.append(now)  # mục heap cũ sẽ được hẹn lại khi tới hạn
            return
        bucket = _Bucket(timestamps=deque([now]))
        plan.pool.buckets[plan.bucket_key] = bucket
        plan.pool.schedule(bucket.expires_at(plan.bucket_key[1]), plan.bucket_key, self._sequence)


class NoopRateLimiter:
    """Dùng khi RATE_LIMIT_ENABLED=false."""

    def hit(self, key: str, rule: RateLimitRule) -> int | None:
        return None

    def hit_all(self, checks: Sequence[RateLimitCheck]) -> int | None:
        return None
