"""Giả mạo X-Forwarded-For và gom IPv6 theo /64 trên endpoint thật."""

from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.modules.auth.rate_limits import REGISTER_PER_IP
from tests.conftest import build_settings, create_schema, sqlite_file_url
from tests.helpers import register_user

pytestmark = pytest.mark.integration

PROXY_IP = "10.0.0.1"


async def make_app(tmp_path: Path, trusted_proxy_ips: list[str]) -> FastAPI:
    app = create_app(
        build_settings(database_url=sqlite_file_url(tmp_path), trusted_proxy_ips=trusted_proxy_ips)
    )
    await create_schema(app)
    return app


async def register_from(
    app: FastAPI, peer: str, email: str, forwarded_for: str | None = None
) -> int:
    transport = ASGITransport(app=app, raise_app_exceptions=False, client=(peer, 50_000))
    headers = {"X-Forwarded-For": forwarded_for} if forwarded_for else {}
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as client:
        return (await register_user(client, email=email)).status_code


async def test_spoofed_forwarded_for_is_ignored_without_trusted_proxy(tmp_path: Path) -> None:
    app = await make_app(tmp_path, trusted_proxy_ips=[])
    statuses = [
        await register_from(
            app, "203.0.113.9", f"u{i}@example.com", forwarded_for=f"198.51.100.{i}"
        )
        for i in range(REGISTER_PER_IP.max_requests + 1)
    ]
    await app.state.engine.dispose()

    assert statuses[-1] == 429  # đổi header không né được giới hạn theo IP


async def test_spoofed_forwarded_for_is_ignored_from_untrusted_peer(tmp_path: Path) -> None:
    app = await make_app(tmp_path, trusted_proxy_ips=[PROXY_IP])
    statuses = [
        await register_from(
            app, "203.0.113.9", f"u{i}@example.com", forwarded_for=f"198.51.100.{i}"
        )
        for i in range(REGISTER_PER_IP.max_requests + 1)
    ]
    await app.state.engine.dispose()

    assert statuses[-1] == 429


async def test_trusted_proxy_separates_real_clients(tmp_path: Path) -> None:
    app = await make_app(tmp_path, trusted_proxy_ips=[PROXY_IP])
    for i in range(REGISTER_PER_IP.max_requests):
        await register_from(app, PROXY_IP, f"a{i}@example.com", forwarded_for="203.0.113.5")

    blocked = await register_from(app, PROXY_IP, "a-extra@example.com", forwarded_for="203.0.113.5")
    other_client = await register_from(app, PROXY_IP, "b@example.com", forwarded_for="203.0.113.6")
    await app.state.engine.dispose()

    assert blocked == 429
    assert other_client == 201  # người dùng khác sau cùng proxy không bị gộp chung


async def test_client_cannot_spoof_through_a_trusted_proxy(tmp_path: Path) -> None:
    """Proxy nối IP thật vào cuối header; giá trị client tự chèn phía trước bị bỏ qua."""
    app = await make_app(tmp_path, trusted_proxy_ips=[PROXY_IP])
    statuses = [
        await register_from(
            app, PROXY_IP, f"u{i}@example.com", forwarded_for=f"198.51.100.{i}, 203.0.113.5"
        )
        for i in range(REGISTER_PER_IP.max_requests + 1)
    ]
    await app.state.engine.dispose()

    assert statuses[-1] == 429


async def test_ipv6_clients_in_the_same_64_share_a_limit(tmp_path: Path) -> None:
    app = await make_app(tmp_path, trusted_proxy_ips=[])
    for i in range(REGISTER_PER_IP.max_requests):
        await register_from(app, f"2001:db8:1:2::{i + 1:x}", f"v{i}@example.com")

    same_prefix = await register_from(app, "2001:db8:1:2::ffff", "v-extra@example.com")
    other_prefix = await register_from(app, "2001:db8:1:3::1", "w@example.com")
    await app.state.engine.dispose()

    assert same_prefix == 429  # đổi địa chỉ trong cùng /64 không né được giới hạn
    assert other_prefix == 201
