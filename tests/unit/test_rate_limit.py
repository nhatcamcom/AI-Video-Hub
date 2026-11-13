import logging
import random
import string
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.core.rate_limit import InMemoryRateLimiter, NoopRateLimiter, RateLimitRule
from app.modules.auth.rate_limits import (
    FORGOT_PASSWORD_PER_EMAIL,
    LOGIN_PER_IP,
    REGISTER_PER_IP,
    RESET_PASSWORD_PER_IP,
)

pytestmark = pytest.mark.unit

RULE = RateLimitRule(max_requests=3, window_seconds=60)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def exhaust(limiter: InMemoryRateLimiter, key: str, rule: RateLimitRule) -> None:
    for _ in range(rule.max_requests):
        assert limiter.hit(key, rule) is None


def random_key(prefix: str) -> str:
    return prefix + "".join(random.choices(string.ascii_lowercase, k=12))  # noqa: S311


# ----------------------------------------------------------------------------- cơ bản
def test_allows_requests_up_to_the_limit() -> None:
    limiter = InMemoryRateLimiter(clock=FakeClock())

    results = [limiter.hit("login:ip:1.2.3.4", RULE) for _ in range(RULE.max_requests)]

    assert results == [None, None, None]


def test_blocks_request_over_the_limit_with_retry_after() -> None:
    clock = FakeClock()
    limiter = InMemoryRateLimiter(clock=clock)
    for _ in range(RULE.max_requests):
        limiter.hit("k", RULE)

    clock.now += 10
    retry_after = limiter.hit("k", RULE)

    assert retry_after == 50  # 60 giây cửa sổ - 10 giây đã trôi qua


def test_allows_again_after_window_expires() -> None:
    clock = FakeClock()
    limiter = InMemoryRateLimiter(clock=clock)
    for _ in range(RULE.max_requests):
        limiter.hit("k", RULE)

    clock.now += RULE.window_seconds + 1

    assert limiter.hit("k", RULE) is None


def test_blocked_requests_do_not_extend_the_window() -> None:
    clock = FakeClock()
    limiter = InMemoryRateLimiter(clock=clock)
    for _ in range(RULE.max_requests):
        limiter.hit("k", RULE)
    for _ in range(5):
        limiter.hit("k", RULE)  # bị chặn, không được ghi nhận

    clock.now += RULE.window_seconds + 1

    assert limiter.hit("k", RULE) is None


def test_sliding_window_frees_only_hits_that_left_the_window() -> None:
    clock = FakeClock()
    limiter = InMemoryRateLimiter(clock=clock)
    for offset in (0, 30, 31):  # 3 lượt ở t=0, t=30, t=31
        clock.now = 1_000.0 + offset
        assert limiter.hit("k", RULE) is None

    clock.now = 1_000.0 + 40
    assert limiter.hit("k", RULE) == 20  # lượt t=0 hết hạn lúc t=60

    clock.now = 1_000.0 + 61  # chỉ lượt t=0 đã ra khỏi cửa sổ → đúng 1 chỗ trống
    assert limiter.hit("k", RULE) is None
    assert limiter.hit("k", RULE) == 29  # lượt t=30 hết hạn lúc t=90
    assert limiter.tracked_key_count == 1  # bucket còn lượt hiệu lực không bị xóa


def test_keys_are_counted_independently() -> None:
    limiter = InMemoryRateLimiter(clock=FakeClock())
    for _ in range(RULE.max_requests):
        limiter.hit("a", RULE)

    assert limiter.hit("a", RULE) is not None
    assert limiter.hit("b", RULE) is None


def test_expired_keys_are_purged_when_tracking_too_many() -> None:
    clock = FakeClock()
    limiter = InMemoryRateLimiter(clock=clock, max_tracked_keys=2)
    limiter.hit("old-1", RULE)
    limiter.hit("old-2", RULE)
    clock.now += RULE.window_seconds + 1

    limiter.hit("new", RULE)

    assert limiter.tracked_key_count == 1


def test_noop_limiter_never_blocks() -> None:
    limiter = NoopRateLimiter()

    assert all(limiter.hit("k", RULE) is None for _ in range(100))


@pytest.mark.parametrize(("max_requests", "window_seconds"), [(0, 60), (-1, 60), (5, 0), (5, -10)])
def test_rule_rejects_non_positive_values(max_requests: int, window_seconds: int) -> None:
    with pytest.raises(ValueError, match="dương"):
        RateLimitRule(max_requests=max_requests, window_seconds=window_seconds)


def test_limiter_rejects_non_positive_capacity() -> None:
    with pytest.raises(ValueError, match="max_tracked_keys"):
        InMemoryRateLimiter(max_tracked_keys=0)


# ----------------------------------------------------------------------------- H-1 và cửa sổ trộn
def test_h1_flooding_short_window_keys_does_not_reset_long_window_counter() -> None:
    """Tái hiện đúng kịch bản H-1 của security review."""
    clock = FakeClock()
    limiter = InMemoryRateLimiter(clock=clock, max_tracked_keys=50)
    exhaust(limiter, "register:ip:198.51.100.7", REGISTER_PER_IP)
    assert limiter.hit("register:ip:198.51.100.7", REGISTER_PER_IP) is not None

    clock.now += 6 * 60  # vẫn trong cửa sổ 1 giờ của register
    for index in range(60):  # nhiều hơn sức chứa, dùng rule login có cửa sổ 5 phút
        limiter.hit(f"login:email:rand{index}@x.test", LOGIN_PER_IP)

    assert limiter.hit("register:ip:198.51.100.7", REGISTER_PER_IP) is not None
    assert limiter.tracked_key_count <= 50


def test_each_rule_keeps_its_own_window() -> None:
    clock = FakeClock()
    limiter = InMemoryRateLimiter(clock=clock, max_tracked_keys=100)
    keys = {
        "login": ("login:ip:1.1.1.1", LOGIN_PER_IP),  # 5 phút
        "forgot": ("forgot:email:a@x.test", FORGOT_PASSWORD_PER_EMAIL),  # 15 phút
        "register": ("register:ip:1.1.1.1", REGISTER_PER_IP),  # 1 giờ
        "reset": ("reset:ip:1.1.1.1", RESET_PASSWORD_PER_IP),  # 1 giờ
    }
    for key, rule in keys.values():
        exhaust(limiter, key, rule)

    def blocked() -> set[str]:
        return {name for name, (key, rule) in keys.items() if limiter.hit(key, rule) is not None}

    assert blocked() == {"login", "forgot", "register", "reset"}
    clock.now += LOGIN_PER_IP.window_seconds + 1
    assert blocked() == {"forgot", "register", "reset"}
    clock.now = 1_000.0 + FORGOT_PASSWORD_PER_EMAIL.window_seconds + 1
    assert blocked() == {"register", "reset"}
    clock.now = 1_000.0 + REGISTER_PER_IP.window_seconds + 1
    assert blocked() == set()


def test_same_key_with_different_rules_is_tracked_separately() -> None:
    limiter = InMemoryRateLimiter(clock=FakeClock())
    strict = RateLimitRule(max_requests=1, window_seconds=60)
    exhaust(limiter, "shared", strict)

    assert limiter.hit("shared", strict) is not None
    assert limiter.hit("shared", RULE) is None


# ----------------------------------------------------------------------------- sức chứa cứng
def test_tracked_keys_never_exceed_capacity() -> None:
    limiter = InMemoryRateLimiter(clock=FakeClock(), max_tracked_keys=50)

    for _ in range(1_000):
        limiter.hit(random_key("login:email:"), LOGIN_PER_IP)
        assert limiter.tracked_key_count <= 50


def test_new_keys_fail_closed_when_full_of_active_keys() -> None:
    clock = FakeClock()
    limiter = InMemoryRateLimiter(clock=clock, max_tracked_keys=3)
    for key in ("a", "b", "c"):
        limiter.hit(key, RULE)

    retry_after = limiter.hit("newcomer", RULE)

    assert retry_after is not None
    assert 0 < retry_after <= RULE.window_seconds
    assert limiter.tracked_key_count == 3
    # Key đang theo dõi vẫn được đếm chính xác (đã 1 lượt → còn 2 lượt)
    assert [limiter.hit("a", RULE) for _ in range(3)] == [None, None, 60]


def test_flooding_random_keys_cannot_reset_a_blocked_key() -> None:
    clock = FakeClock()
    limiter = InMemoryRateLimiter(clock=clock, max_tracked_keys=50)
    target = "login:email:victim@example.com"
    exhaust(limiter, target, RULE)

    for _ in range(500):
        limiter.hit(random_key("login:email:"), RULE)

    assert limiter.hit(target, RULE) is not None
    clock.now += RULE.window_seconds + 1
    assert limiter.hit(target, RULE) is None  # chỉ được mở lại khi đúng hết cửa sổ của chính nó


def test_capacity_is_released_when_keys_expire() -> None:
    clock = FakeClock()
    limiter = InMemoryRateLimiter(clock=clock, max_tracked_keys=3)
    for key in ("a", "b", "c"):
        limiter.hit(key, RULE)
    assert limiter.hit("d", RULE) is not None

    clock.now += RULE.window_seconds + 1

    assert limiter.hit("d", RULE) is None
    assert limiter.tracked_key_count == 1


def test_saturation_warning_does_not_log_keys(caplog: pytest.LogCaptureFixture) -> None:
    limiter = InMemoryRateLimiter(clock=FakeClock(), max_tracked_keys=1)
    limiter.hit("login:email:secret.person@example.com", RULE)

    with caplog.at_level(logging.WARNING):
        limiter.hit("login:ip:203.0.113.77", RULE)
        limiter.hit("login:ip:203.0.113.78", RULE)

    assert "Rate limiter is full" in caplog.text
    assert caplog.text.count("Rate limiter is full") == 1  # không spam log
    assert "secret.person@example.com" not in caplog.text
    assert "203.0.113.77" not in caplog.text


# ----------------------------------------------------------------------------- đồng thời
def test_concurrent_hits_never_exceed_the_limit() -> None:
    limiter = InMemoryRateLimiter()  # đồng hồ thật
    rule = RateLimitRule(max_requests=50, window_seconds=60)

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: limiter.hit("hot-key", rule), range(400)))

    assert sum(result is None for result in results) == rule.max_requests
