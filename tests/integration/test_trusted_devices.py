"""Cookie "thiết bị quen": ngân sách đăng nhập riêng, gắn với một tài khoản, thu hồi khi reset."""

from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import func, select

from app.main import create_app
from app.modules.auth.models import TrustedDevice
from app.modules.auth.rate_limits import LOGIN_PER_DEVICE, LOGIN_PER_EMAIL, LOGIN_PER_IP
from app.modules.auth.service import MAX_TRUSTED_DEVICES_PER_USER
from tests.conftest import build_production_settings, create_schema, sqlite_file_url
from tests.helpers import (
    DEFAULT_EMAIL,
    DEFAULT_PASSWORD,
    extract_reset_token,
    login_user,
    register_user,
)

pytestmark = pytest.mark.integration

DEVICE_COOKIE = "avh_device"
WRONG_PASSWORD = "wrong-password-123"


def client_from(app: FastAPI, address: str) -> AsyncClient:
    transport = ASGITransport(app=app, raise_app_exceptions=False, client=(address, 50_000))
    return AsyncClient(transport=transport, base_url="http://test")


async def device_count(app: FastAPI) -> int:
    async with app.state.session_factory() as session:
        result = await session.execute(select(func.count()).select_from(TrustedDevice))
        return int(result.scalar_one())


async def exhaust_account_budget(app: FastAPI, email: str = DEFAULT_EMAIL) -> None:
    for index in range(LOGIN_PER_EMAIL.max_requests):
        async with client_from(app, f"198.51.100.{index + 1}") as attacker:
            await login_user(attacker, email=email, password=WRONG_PASSWORD)


async def test_successful_login_sets_an_httponly_device_cookie(client: AsyncClient) -> None:
    await register_user(client)

    response = await login_user(client)

    device_cookie = next(
        header
        for header in response.headers.get_list("set-cookie")
        if header.startswith(DEVICE_COOKIE)
    )
    assert "httponly" in device_cookie.lower()
    assert "samesite=lax" in device_cookie.lower()
    assert "max-age=2592000" in device_cookie.lower()  # 30 ngày


async def test_failed_login_sets_no_device_cookie(client: AsyncClient) -> None:
    await register_user(client)

    response = await login_user(client, password=WRONG_PASSWORD)

    assert DEVICE_COOKIE not in response.headers.get("set-cookie", "")


async def test_device_cookie_uses_host_prefix_in_production(tmp_path: Path) -> None:
    app = create_app(build_production_settings(database_url=sqlite_file_url(tmp_path)))
    await create_schema(app)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with AsyncClient(transport=transport, base_url="https://test") as client:
            await register_user(client)
            response = await login_user(client)
    finally:
        await app.state.engine.dispose()

    cookies = response.headers.get_list("set-cookie")
    assert any(cookie.startswith(f"__Host-{DEVICE_COOKIE}=") for cookie in cookies)
    assert all("secure" in cookie.lower() for cookie in cookies)


async def test_device_of_another_account_gives_no_privilege(app: FastAPI) -> None:
    async with client_from(app, "192.0.2.10") as attacker:
        await register_user(attacker, email="attacker@example.com")
        assert (await login_user(attacker, email="attacker@example.com")).status_code == 200
        await register_user(attacker, email=DEFAULT_EMAIL)
        await exhaust_account_budget(app)

        # Cookie thiết bị của tài khoản kẻ tấn công không mở được ngân sách của nạn nhân
        response = await login_user(attacker, password=WRONG_PASSWORD)

    assert response.status_code == 429


async def test_known_device_has_its_own_bounded_budget(app: FastAPI) -> None:
    async with client_from(app, "192.0.2.10") as owner:
        await register_user(owner)
        assert (await login_user(owner)).status_code == 200

        statuses = [
            (await login_user(owner, password=WRONG_PASSWORD)).status_code
            for _ in range(LOGIN_PER_DEVICE.max_requests + 1)
        ]

    assert statuses == [401] * LOGIN_PER_DEVICE.max_requests + [429]  # cookie bị lộ cũng có trần


async def test_known_device_is_not_blocked_by_its_ip_or_network_limit(app: FastAPI) -> None:
    """Giảm thiểu L-7/CGNAT: người khác cùng IP (hoặc cùng /48) đốt hết giới hạn IP."""
    async with client_from(app, "203.0.113.50") as owner:
        await register_user(owner)
        assert (await login_user(owner)).status_code == 200

        async with client_from(app, "203.0.113.50") as neighbour:  # cùng IP NAT
            for index in range(LOGIN_PER_IP.max_requests):
                await login_user(neighbour, email=f"n{index}@example.com", password=WRONG_PASSWORD)
            assert (await login_user(neighbour, email="n@example.com")).status_code == 429

        assert (await login_user(owner)).status_code == 200


async def test_password_reset_revokes_known_devices_and_unlocks_the_account(app: FastAPI) -> None:
    async with client_from(app, "192.0.2.10") as owner:
        await register_user(owner)
        await login_user(owner)
        assert await device_count(app) == 1
        await exhaust_account_budget(app)

        await owner.post("/api/v1/auth/forgot-password", json={"email": DEFAULT_EMAIL})
        reset = await owner.post(
            "/api/v1/auth/reset-password",
            json={"token": extract_reset_token(app), "new_password": "a-brand-new-password-1"},
        )
        assert reset.status_code == 200
        assert await device_count(app) == 0

    async with client_from(app, "192.0.2.77") as new_browser:  # thiết bị mới, không cookie
        response = await login_user(new_browser, password="a-brand-new-password-1")

    assert response.status_code == 200  # chứng minh sở hữu email → mở lại bộ đếm


async def test_devices_per_account_are_capped(app: FastAPI) -> None:
    async with client_from(app, "192.0.2.10") as first:
        await register_user(first)
    for index in range(MAX_TRUSTED_DEVICES_PER_USER + 3):
        async with client_from(app, f"192.0.2.{20 + index}") as browser:
            response: Response = await login_user(browser)
            assert response.status_code == 200

    assert await device_count(app) == MAX_TRUSTED_DEVICES_PER_USER


async def test_login_still_works_with_a_garbage_device_cookie(client: AsyncClient) -> None:
    await register_user(client)
    client.cookies.set(DEVICE_COOKIE, "not-a-real-token")

    assert (await login_user(client)).status_code == 200


async def test_device_cookie_survives_logout(client: AsyncClient) -> None:
    await register_user(client)
    await login_user(client)
    csrf = client.cookies["avh_csrf"]

    response = await client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf})

    assert response.status_code == 204
    assert DEVICE_COOKIE not in response.headers.get("set-cookie", "")
    assert DEVICE_COOKIE in client.cookies
    assert (
        await login_user(client, email=DEFAULT_EMAIL, password=DEFAULT_PASSWORD)
    ).status_code == 200
