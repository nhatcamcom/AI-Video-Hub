import asyncio

import pytest
from httpx import AsyncClient

from app.modules.auth.rate_limits import (
    FORGOT_PASSWORD_PER_EMAIL,
    LOGIN_PER_EMAIL,
    REGISTER_PER_IP,
)
from tests.helpers import DEFAULT_EMAIL, login_user, register_user

pytestmark = pytest.mark.integration


async def test_login_is_rate_limited_per_email(client: AsyncClient) -> None:
    await register_user(client)
    for _ in range(LOGIN_PER_EMAIL.max_requests):
        assert (await login_user(client, password="wrong-password-123")).status_code == 401

    response = await login_user(client, password="wrong-password-123")

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limit_exceeded"
    assert int(response.headers["retry-after"]) > 0


async def test_concurrent_login_attempts_cannot_exceed_the_limit(client: AsyncClient) -> None:
    await register_user(client)
    attempts = LOGIN_PER_EMAIL.max_requests * 2

    responses = await asyncio.gather(
        *(login_user(client, password="wrong-password-123") for _ in range(attempts))
    )

    statuses = sorted(response.status_code for response in responses)
    assert statuses.count(401) == LOGIN_PER_EMAIL.max_requests
    assert statuses.count(429) == attempts - LOGIN_PER_EMAIL.max_requests


async def test_rate_limited_login_blocks_even_correct_password(client: AsyncClient) -> None:
    """Brute-force không thể 'thử nốt' mật khẩu đúng sau khi bị chặn."""
    await register_user(client)
    for _ in range(LOGIN_PER_EMAIL.max_requests):
        await login_user(client, password="wrong-password-123")

    assert (await login_user(client)).status_code == 429


async def test_login_rate_limit_does_not_block_other_accounts(client: AsyncClient) -> None:
    await register_user(client, email="victim@example.com")
    await register_user(client, email="other@example.com")
    for _ in range(LOGIN_PER_EMAIL.max_requests):
        await login_user(client, email="victim@example.com", password="wrong-password-123")

    assert (await login_user(client, email="other@example.com")).status_code == 200


async def test_register_is_rate_limited_per_ip(client: AsyncClient) -> None:
    for index in range(REGISTER_PER_IP.max_requests):
        assert (await register_user(client, email=f"user{index}@example.com")).status_code == 201

    response = await register_user(client, email="one-too-many@example.com")

    assert response.status_code == 429
    assert "retry-after" in response.headers


async def test_forgot_password_is_rate_limited_per_email(client: AsyncClient) -> None:
    await register_user(client)
    for _ in range(FORGOT_PASSWORD_PER_EMAIL.max_requests):
        response = await client.post("/api/v1/auth/forgot-password", json={"email": DEFAULT_EMAIL})
        assert response.status_code == 202

    response = await client.post("/api/v1/auth/forgot-password", json={"email": DEFAULT_EMAIL})

    assert response.status_code == 429
