"""M-1: sức chứa riêng mỗi rule, rule phụ bỏ qua key mới khi đầy, kiểm tra nhiều rule nguyên tử."""

import logging
import random
import string
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.core.rate_limit import (
    InMemoryRateLimiter,
    NoopRateLimiter,
    RateLimitCheck,
    RateLimitRule,
    WhenFull,
    allocate_capacity,
)

pytestmark = pytest.mark.unit

PRIMARY = RateLimitRule(max_requests=3, window_seconds=60, name="primary")
SECONDARY = RateLimitRule(
    max_requests=2, window_seconds=600, name="secondary", when_full=WhenFull.SKIP
)
OTHER = RateLimitRule(max_requests=3, window_seconds=3_600, name="other")
UNRESERVED = RateLimitRule(max_requests=3, window_seconds=60)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def partitioned(clock: FakeClock, capacity: int = 5) -> InMemoryRateLimiter:
    return InMemoryRateLimiter(
        clock=clock,
        max_tracked_keys=capacity * 3,
        reserved_capacity={PRIMARY: capacity, SECONDARY: capacity, OTHER: capacity},
    )


def exhaust(limiter: InMemoryRateLimiter, key: str, rule: RateLimitRule) -> None:
    for _ in range(rule.max_requests):
        assert limiter.hit(key, rule) is None


def random_key(prefix: str) -> str:
    return prefix + "".join(random.choices(string.ascii_lowercase, k=12))  # noqa: S311


def with_email(ip_key: str, email_key: str) -> list[RateLimitCheck]:
    return [RateLimitCheck(ip_key, PRIMARY), RateLimitCheck(email_key, SECONDARY)]


# ----------------------------------------------------------------------------- pool riêng
def test_filling_one_rule_does_not_take_capacity_from_another_rule() -> None:
    limiter = partitioned(FakeClock())
    for index in range(50):
        limiter.hit(f"primary:{index}", PRIMARY)

    assert limiter.tracked_key_count_for(PRIMARY) == 5
    assert limiter.hit("other:newcomer", OTHER) is None
    assert limiter.hit("primary:newcomer", PRIMARY) is not None  # rule chính vẫn fail-closed


def test_unreserved_rules_share_the_remaining_capacity() -> None:
    limiter = InMemoryRateLimiter(
        clock=FakeClock(), max_tracked_keys=12, reserved_capacity={PRIMARY: 10}
    )
    assert (limiter.capacity_for(PRIMARY), limiter.capacity_for(UNRESERVED)) == (10, 2)
    limiter.hit("x", UNRESERVED)
    limiter.hit("y", UNRESERVED)

    assert limiter.hit("z", UNRESERVED) is not None
    assert limiter.hit("primary:1", PRIMARY) is None


def test_zero_remaining_capacity_rejects_unreserved_rules() -> None:
    limiter = InMemoryRateLimiter(
        clock=FakeClock(), max_tracked_keys=10, reserved_capacity={PRIMARY: 10}
    )

    assert limiter.hit("anything", UNRESERVED) == UNRESERVED.window_seconds


@pytest.mark.parametrize("reserved", [{PRIMARY: 11}, {PRIMARY: 6, OTHER: 5}, {PRIMARY: 0}])
def test_limiter_rejects_invalid_reserved_capacity(reserved: dict[RateLimitRule, int]) -> None:
    with pytest.raises(ValueError, match="sức chứa"):
        InMemoryRateLimiter(max_tracked_keys=10, reserved_capacity=reserved)


def test_rule_requires_a_name() -> None:
    with pytest.raises(ValueError, match="tên"):
        RateLimitRule(max_requests=1, window_seconds=1, name="")


def test_allocate_capacity_splits_by_share_without_exceeding_total() -> None:
    allocated = allocate_capacity(1_000, {PRIMARY: 1, SECONDARY: 1, OTHER: 1})

    assert allocated == {PRIMARY: 333, SECONDARY: 333, OTHER: 333}


def test_allocate_capacity_gives_every_rule_at_least_one_slot() -> None:
    assert allocate_capacity(1, {PRIMARY: 1, SECONDARY: 1_000})[PRIMARY] == 1


@pytest.mark.parametrize("shares", [{}, {PRIMARY: 0}, {PRIMARY: -1, OTHER: 5}])
def test_allocate_capacity_rejects_invalid_shares(shares: dict[RateLimitRule, int]) -> None:
    with pytest.raises(ValueError, match="tỉ trọng"):
        allocate_capacity(100, shares)


# ----------------------------------------------------------------------------- rule phụ (SKIP)
def test_skip_rule_lets_new_keys_through_untracked_when_full() -> None:
    limiter = partitioned(FakeClock())
    for index in range(5):
        limiter.hit(f"email:{index}", SECONDARY)

    results = [limiter.hit(f"email:new{index}", SECONDARY) for index in range(10)]

    assert results == [None] * 10
    assert limiter.tracked_key_count_for(SECONDARY) == 5  # không tạo bucket vượt sức chứa


def test_primary_rule_still_applies_when_secondary_is_skipped() -> None:
    limiter = partitioned(FakeClock())
    for index in range(5):
        limiter.hit(f"email:{index}", SECONDARY)

    results = [
        limiter.hit_all(with_email("ip:1", f"email:x{index}"))
        for index in range(PRIMARY.max_requests + 1)
    ]

    assert results[:-1] == [None] * PRIMARY.max_requests
    assert results[-1] is not None  # đổi email không né được rule theo IP


def test_tracked_secondary_key_is_still_counted_exactly_when_full() -> None:
    clock = FakeClock()
    limiter = partitioned(clock)
    assert limiter.hit("email:victim", SECONDARY) is None
    for index in range(20):  # làm đầy pool email bằng key ngẫu nhiên
        limiter.hit(f"email:spray{index}", SECONDARY)

    assert limiter.hit("email:victim", SECONDARY) is None  # lượt 2 / 2
    assert limiter.hit("email:victim", SECONDARY) == SECONDARY.window_seconds
    clock.now += SECONDARY.window_seconds - 1
    assert limiter.hit("email:victim", SECONDARY) is not None  # chưa hết cửa sổ → vẫn chặn
    clock.now += 2
    assert limiter.hit("email:victim", SECONDARY) is None


def test_skip_saturation_warning_does_not_log_keys(caplog: pytest.LogCaptureFixture) -> None:
    limiter = partitioned(FakeClock(), capacity=1)
    limiter.hit("email:first.person@example.com", SECONDARY)

    with caplog.at_level(logging.WARNING):
        limiter.hit("email:second.person@example.com", SECONDARY)
        limiter.hit("email:third.person@example.com", SECONDARY)

    assert caplog.text.count("Rate limiter is full for 'secondary'") == 1
    assert "person@example.com" not in caplog.text


# ----------------------------------------------------------------------------- nguyên tử
def test_blocked_request_records_nothing_in_any_rule() -> None:
    limiter = partitioned(FakeClock())
    exhaust(limiter, "ip:1", PRIMARY)

    for index in range(10):
        assert limiter.hit_all(with_email("ip:1", f"email:{index}")) is not None

    assert limiter.tracked_key_count_for(SECONDARY) == 0  # lượt bị chặn không tạo bucket


def test_blocked_secondary_does_not_spend_primary_budget() -> None:
    limiter = partitioned(FakeClock())
    exhaust(limiter, "email:victim", SECONDARY)

    for _ in range(10):
        limiter.hit_all(with_email("ip:1", "email:victim"))

    assert [limiter.hit("ip:1", PRIMARY) for _ in range(4)] == [None, None, None, 60]


def test_retry_after_is_the_longest_wait_across_blocking_rules() -> None:
    limiter = partitioned(FakeClock())
    exhaust(limiter, "ip:1", PRIMARY)
    exhaust(limiter, "other:1", OTHER)

    retry_after = limiter.hit_all(
        [RateLimitCheck("ip:1", PRIMARY), RateLimitCheck("other:1", OTHER)]
    )

    assert retry_after == OTHER.window_seconds


def test_new_buckets_in_one_call_respect_capacity_together() -> None:
    limiter = InMemoryRateLimiter(clock=FakeClock(), max_tracked_keys=3)
    limiter.hit("a", UNRESERVED)
    limiter.hit("b", UNRESERVED)  # còn đúng 1 chỗ
    other_rule = RateLimitRule(max_requests=3, window_seconds=60, name="other-unreserved")

    checks = [RateLimitCheck("c", UNRESERVED), RateLimitCheck("d", other_rule)]

    assert limiter.hit_all(checks) is not None
    assert limiter.tracked_key_count == 2


def test_duplicate_checks_in_one_call_are_rejected() -> None:
    limiter = InMemoryRateLimiter(clock=FakeClock())

    with pytest.raises(ValueError, match="hai lần"):
        limiter.hit_all([RateLimitCheck("a", PRIMARY), RateLimitCheck("a", PRIMARY)])


def test_noop_limiter_never_blocks_multiple_checks() -> None:
    assert NoopRateLimiter().hit_all(with_email("a", "b")) is None


# ----------------------------------------------------------------------------- cửa sổ trượt
def test_unexpired_buckets_are_never_removed_to_make_room() -> None:
    clock = FakeClock()
    limiter = partitioned(clock)
    for index in range(5):
        exhaust(limiter, f"primary:{index}", PRIMARY)

    clock.now += PRIMARY.window_seconds - 1
    for index in range(100):
        limiter.hit(f"primary:flood{index}", PRIMARY)

    assert limiter.tracked_key_count_for(PRIMARY) == 5
    assert all(limiter.hit(f"primary:{index}", PRIMARY) == 1 for index in range(5))


def test_sliding_window_is_exact_across_rules_in_one_call() -> None:
    clock = FakeClock()
    limiter = partitioned(clock)
    checks = with_email("ip:1", "email:1")
    assert limiter.hit_all(checks) is None  # t=0
    clock.now += 30
    assert limiter.hit_all(checks) is None  # t=30: email đã dùng hết 2 lượt / 600 giây

    clock.now += 31  # t=61: lượt IP đầu đã ra khỏi cửa sổ 60 giây, email thì chưa
    assert limiter.hit_all(checks) == SECONDARY.window_seconds - 61
    # Lượt bị chặn ở trên không tiêu ngân sách IP: còn 2 chỗ (lượt t=30 vẫn trong cửa sổ)
    assert [limiter.hit("ip:1", PRIMARY) for _ in range(3)] == [None, None, 29]


# ----------------------------------------------------------------------------- đồng thời
def test_concurrent_mixed_rules_never_exceed_per_rule_capacity() -> None:
    limiter = partitioned(FakeClock(), capacity=20)

    def attack(index: int) -> int | None:
        return limiter.hit_all(
            [
                RateLimitCheck(f"ip:{index % 30}", PRIMARY),
                RateLimitCheck(random_key("email:"), SECONDARY),
                RateLimitCheck(f"other:{index}", OTHER),
            ]
        )

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(attack, range(400)))

    for rule in (PRIMARY, SECONDARY, OTHER):
        assert limiter.tracked_key_count_for(rule) <= 20
    assert limiter.tracked_key_count <= 60


def test_concurrent_hits_on_one_key_across_rules_never_exceed_the_limit() -> None:
    limiter = partitioned(FakeClock(), capacity=50)
    checks = [RateLimitCheck("ip:hot", OTHER), RateLimitCheck("email:hot", SECONDARY)]

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: limiter.hit_all(checks), range(400)))

    assert sum(result is None for result in results) == SECONDARY.max_requests
