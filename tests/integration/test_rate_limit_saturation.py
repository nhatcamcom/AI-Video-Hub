"""M-1: làm đầy bộ đếm rate limit không được khóa auth của người dùng mới.

Tái hiện kịch bản của security review trên endpoint thật: kẻ tấn công tạo rất nhiều bucket
`login:email:*` (mỗi email ngẫu nhiên một bucket) và nhiều /64 trong cùng một mạng IPv6.
"""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.rate_limit import InMemoryRateLimiter, allocate_capacity
from app.main import create_app
from app.modules.auth.rate_limits import (
    AUTH_CAPACITY_SHARES,
    IPV6_NETWORK_BUDGET_MULTIPLIER,
    LOGIN_PER_EMAIL,
    LOGIN_PER_IP,
    RESET_PASSWORD_PER_IP,
)
from tests.conftest import build_settings, create_schema, sqlite_file_url
from tests.helpers import DEFAULT_EMAIL, login_user, register_user

pytestmark = pytest.mark.integration

MAX_KEYS = 200  # nhỏ để làm đầy nhanh; tỉ lệ chia theo rule giống production
ATTACKER_IP = "198.51.100.66"
NEW_CLIENT_IP = "203.0.113.20"
WRONG_PASSWORD = "wrong-password-123"


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[FastAPI]:
    application = create_app(
        build_settings(database_url=sqlite_file_url(tmp_path), rate_limit_max_keys=MAX_KEYS)
    )
    await create_schema(application)
    yield application
    await application.state.engine.dispose()


def client_from(app: FastAPI, peer: str) -> AsyncClient:
    transport = ASGITransport(app=app, raise_app_exceptions=False, client=(peer, 50_000))
    return AsyncClient(transport=transport, base_url="http://test")


def flood_login_emails(app: FastAPI, count: int = MAX_KEYS) -> None:
    """Kịch bản M-1: làm đầy bảng in-memory bằng khóa email ngẫu nhiên.

    Giới hạn theo email nay nằm trong database nên khóa này không còn chiếm chỗ của rule nào
    (rơi vào phần dùng chung, sức chứa 0); test giữ lại để chứng minh M-1 không quay lại.
    """
    for index in range(count):
        app.state.rate_limiter.hit(
            f"login:email:spray{index}@attacker.example.net", LOGIN_PER_EMAIL
        )


async def reset_status(client: AsyncClient) -> int:
    response = await client.post(
        "/api/v1/auth/reset-password",
        json={"token": "x" * 43, "new_password": "brand-new-password-1"},
    )
    return response.status_code


# ----------------------------------------------------------------------------- tách capacity
async def test_login_email_flood_does_not_block_new_clients_on_other_endpoints(
    app: FastAPI,
) -> None:
    flood_login_emails(app)

    async with client_from(app, NEW_CLIENT_IP) as client:
        register = (await register_user(client)).status_code
        reset = await reset_status(client)
        forgot = (
            await client.post("/api/v1/auth/forgot-password", json={"email": DEFAULT_EMAIL})
        ).status_code
        login = (await login_user(client)).status_code

    assert (register, reset, forgot, login) == (201, 400, 202, 200)


async def test_new_email_passes_when_email_capacity_is_full_but_ip_rule_still_applies(
    app: FastAPI,
) -> None:
    flood_login_emails(app)

    async with client_from(app, ATTACKER_IP) as client:
        statuses = [
            (
                await login_user(client, email=f"new{i}@example.com", password=WRONG_PASSWORD)
            ).status_code
            for i in range(LOGIN_PER_IP.max_requests + 1)
        ]

    # Email mới không có bucket vẫn đi tiếp tới kiểm tra mật khẩu (401), rule IP chặn lượt thừa
    assert statuses[:-1] == [401] * LOGIN_PER_IP.max_requests
    assert statuses[-1] == 429


async def test_tracked_email_stays_limited_after_email_capacity_fills(app: FastAPI) -> None:
    async with client_from(app, NEW_CLIENT_IP) as client:
        await register_user(client)
    async with client_from(app, ATTACKER_IP) as attacker:
        for _ in range(LOGIN_PER_EMAIL.max_requests):
            await login_user(attacker, password=WRONG_PASSWORD)

    flood_login_emails(app)

    async with client_from(app, "192.0.2.77") as other_ip:
        # Bucket của email nạn nhân đã có từ trước → vẫn bị đếm, không thể bị đẩy ra để mở khóa
        assert (await login_user(other_ip)).status_code == 429


# ----------------------------------------------------------------------------- IPv6 /48
async def test_many_64s_in_the_same_48_share_a_network_budget(app: FastAPI) -> None:
    per_client = RESET_PASSWORD_PER_IP.max_requests
    accepted = 0
    for subnet in range(IPV6_NETWORK_BUDGET_MULTIPLIER + 2):  # 12 /64 trong cùng 2001:db8:aa::/48
        async with client_from(app, f"2001:db8:aa:{subnet:x}::1") as client:
            for _ in range(per_client):
                accepted += await reset_status(client) == 400

    assert accepted == IPV6_NETWORK_BUDGET_MULTIPLIER * per_client


async def test_different_48s_are_not_merged(app: FastAPI) -> None:
    per_client = RESET_PASSWORD_PER_IP.max_requests
    for subnet in range(IPV6_NETWORK_BUDGET_MULTIPLIER + 1):
        async with client_from(app, f"2001:db8:aa:{subnet:x}::1") as client:
            for _ in range(per_client):
                await reset_status(client)

    async with client_from(app, "2001:db8:aa:ff::1") as same_network:
        assert await reset_status(same_network) == 429
    async with client_from(app, "2001:db8:bb::1") as other_network:
        assert await reset_status(other_network) == 400


def test_app_reserves_capacity_for_every_auth_rule(app: FastAPI) -> None:
    limiter = app.state.rate_limiter
    assert isinstance(limiter, InMemoryRateLimiter)
    expected = allocate_capacity(MAX_KEYS, AUTH_CAPACITY_SHARES)

    assert {rule: limiter.capacity_for(rule) for rule in AUTH_CAPACITY_SHARES} == expected
    assert sum(expected.values()) <= MAX_KEYS


async def test_ipv4_and_ipv4_mapped_clients_keep_per_address_limits(app: FastAPI) -> None:
    per_client = RESET_PASSWORD_PER_IP.max_requests
    async with client_from(app, "203.0.113.5") as client:
        for _ in range(per_client):
            assert await reset_status(client) == 400
    async with client_from(app, "::ffff:203.0.113.5") as mapped_same:
        assert await reset_status(mapped_same) == 429  # cùng địa chỉ IPv4
    async with client_from(app, "203.0.113.6") as neighbour:
        assert await reset_status(neighbour) == 400  # IPv4 không bị gom theo mạng
