"""Mô phỏng tấn công vào lớp giới hạn IP / mạng (in-memory) cấu hình như production.

Dùng đúng limiter mà `create_app` dựng và đúng các khóa router tạo (`login_checks`, ...), thay
đồng hồ thật bằng đồng hồ giả để chạy 15 phút trong vài giây. Lớp theo tài khoản (database) được
kiểm thử riêng qua endpoint thật trong `test_account_bruteforce.py`.
"""

import ipaddress

import pytest

from app.core.client_ip import RateLimitSubject
from app.core.rate_limit import InMemoryRateLimiter, RateLimitCheck
from app.main import create_rate_limiter
from app.modules.auth.rate_limits import (
    FORGOT_PASSWORD_LIMIT,
    FORGOT_PASSWORD_PER_IP,
    LOGIN_LIMIT,
    LOGIN_PER_IP,
    MINUTE,
    REGISTER_PER_IP,
    RESET_PASSWORD_PER_IP,
    forgot_password_checks,
    login_checks,
    register_checks,
    reset_password_checks,
)
from tests.conftest import build_settings

pytestmark = pytest.mark.integration

LOGIN_WINDOW_STARTS_MINUTES = (0, 5, 10)  # một đợt login đầy ngân sách mỗi cửa sổ 5 phút


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class Attack:
    """Mỗi request là một lượt `hit_all`, đúng như router gọi."""

    def __init__(self, limiter: InMemoryRateLimiter) -> None:
        self.limiter = limiter
        self.sent = 0
        self.accepted = 0

    def send(self, checks: list[RateLimitCheck]) -> bool:
        self.sent += 1
        is_accepted = self.limiter.hit_all(checks) is None
        self.accepted += is_accepted
        return is_accepted

    def login_burst(self, subject: RateLimitSubject) -> None:
        for _ in range(LOGIN_PER_IP.max_requests):
            self.send(login_checks(subject))

    def forgot_burst(self, subject: RateLimitSubject) -> None:
        for _ in range(FORGOT_PASSWORD_PER_IP.max_requests):
            self.send(forgot_password_checks(subject))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def limiter(clock: FakeClock) -> InMemoryRateLimiter:
    production_like = create_rate_limiter(build_settings(), clock=clock)
    assert isinstance(production_like, InMemoryRateLimiter)
    return production_like


def run_campaign(clock: FakeClock, subjects: list[RateLimitSubject], attack: Attack) -> None:
    for minute in LOGIN_WINDOW_STARTS_MINUTES:
        clock.now = 1_000.0 + minute * MINUTE
        for subject in subjects:
            attack.login_burst(subject)
            if minute == 0:
                attack.forgot_burst(subject)


def assert_new_client_is_served(limiter: InMemoryRateLimiter, client_ip: str) -> None:
    subject = RateLimitSubject.from_client_ip(client_ip)
    results = {
        "login": limiter.hit_all(login_checks(subject)),
        "register": limiter.hit_all(register_checks(subject)),
        "forgot": limiter.hit_all(forgot_password_checks(subject)),
        "reset": limiter.hit_all(reset_password_checks(subject)),
    }
    assert results == dict.fromkeys(results)  # mọi giá trị None = được phục vụ, không 429


def test_review_scenario_one_ipv6_56_cannot_lock_out_new_users(
    clock: FakeClock, limiter: InMemoryRateLimiter
) -> None:
    """IPv6 /56 = 256 dải /64; mỗi /64 đốt hết ngân sách login + forgot."""
    network = ipaddress.ip_network("2001:db8:aa00::/56")
    subjects = [
        RateLimitSubject.from_client_ip(str(subnet.network_address + 1))
        for subnet in network.subnets(new_prefix=64)
    ]
    attack = Attack(limiter)

    run_campaign(clock, subjects, attack)
    clock.now += 1

    assert attack.sent == 25_600
    # Cả /56 nằm trong một /48 nên chỉ được ngân sách của một mạng cho mỗi cửa sổ
    assert attack.accepted == (
        len(LOGIN_WINDOW_STARTS_MINUTES) * LOGIN_LIMIT.ipv6_network.max_requests
        + FORGOT_PASSWORD_LIMIT.ipv6_network.max_requests
    )
    assert_new_client_is_served(limiter, "203.0.113.9")
    assert_new_client_is_served(limiter, "2001:db8:bb::1")  # /48 khác không bị gom chung


def test_ipv4_botnet_gets_exactly_its_per_ip_budget(
    clock: FakeClock, limiter: InMemoryRateLimiter
) -> None:
    botnet = [
        RateLimitSubject.from_client_ip(str(ipaddress.IPv4Address("198.18.0.0") + index))
        for index in range(900)
    ]
    attack = Attack(limiter)

    run_campaign(clock, botnet, attack)
    clock.now += 1

    for rule in (LOGIN_PER_IP, REGISTER_PER_IP, FORGOT_PASSWORD_PER_IP, RESET_PASSWORD_PER_IP):
        assert limiter.tracked_key_count_for(rule) < limiter.capacity_for(rule)
    assert attack.accepted == len(botnet) * (
        len(LOGIN_WINDOW_STARTS_MINUTES) * LOGIN_PER_IP.max_requests
        + FORGOT_PASSWORD_PER_IP.max_requests
    )
    assert attack.send(login_checks(botnet[0])) is False
    assert_new_client_is_served(limiter, "203.0.113.10")


def test_many_ipv6_48s_filling_the_64_table_cannot_lock_out_new_users(
    clock: FakeClock, limiter: InMemoryRateLimiter
) -> None:
    """Nhiều /48 (vd. tunnel broker miễn phí), mỗi /64 chỉ gửi 1 request để tạo bucket mới.

    Rule /64 bỏ qua /64 mới khi đầy; rule /48 (fail-closed) vẫn giữ mỗi mạng trong ngân sách.
    """
    login_ip6 = LOGIN_LIMIT.ipv6_client
    per_network = LOGIN_LIMIT.ipv6_network.max_requests
    networks_needed = limiter.capacity_for(login_ip6) // per_network + 5
    attack = Attack(limiter)
    for network_index in range(networks_needed):
        for subnet in range(per_network + 50):  # vượt cả ngân sách của mạng
            subject = RateLimitSubject.from_client_ip(f"2001:db8:{network_index:x}:{subnet:x}::1")
            attack.send(login_checks(subject))
    clock.now += 1

    assert limiter.tracked_key_count_for(login_ip6) == limiter.capacity_for(login_ip6)
    assert attack.accepted == networks_needed * per_network  # không mạng nào vượt ngân sách /48
    attacker_64 = RateLimitSubject.from_client_ip("2001:db8:0:ffff::1")
    assert attack.send(login_checks(attacker_64)) is False
    assert limiter.tracked_key_count_for(LOGIN_PER_IP) < limiter.capacity_for(LOGIN_PER_IP)
    assert_new_client_is_served(limiter, "203.0.113.11")
    assert_new_client_is_served(limiter, "2001:db8:cafe::1")
