"""M-2 và các lỗi liên quan: bảo vệ brute-force theo TÀI KHOẢN, không phụ thuộc pool in-memory.

Mọi kịch bản đi qua endpoint thật. Kẻ tấn công dùng nhiều IP (mỗi IP vẫn trong giới hạn IP) và
làm đầy bộ đếm bằng email giả trước khi dò tài khoản mục tiêu.
"""

import asyncio
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Literal

import pytest
from argon2 import PasswordHasher
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text

from app.core.email import InMemoryEmailSender
from app.main import create_app
from app.modules.auth import security
from app.modules.auth.rate_limits import FORGOT_PASSWORD_PER_EMAIL, LOGIN_PER_EMAIL
from tests.conftest import build_settings, create_schema, sqlite_file_url
from tests.helpers import DEFAULT_EMAIL, DEFAULT_PASSWORD, register_user

pytestmark = pytest.mark.integration

MAX_KEYS = 100  # bảng in-memory nhỏ để làm đầy nhanh
WRONG_PASSWORD = "wrong-password-123"
FAKE_EMAIL_FLOOD = 40  # nhiều hơn mọi chỗ dành cho email trong bảng in-memory cũ
# 4 lượt Argon2 song song x 64 MiB = 256 MiB trần bộ nhớ cho việc băm mật khẩu
ARGON2_PARALLEL_LIMIT = 4


def peer(index: int) -> str:
    return f"198.51.{100 + index // 250}.{index % 250 + 1}"


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[FastAPI]:
    application = create_app(
        build_settings(database_url=sqlite_file_url(tmp_path), rate_limit_max_keys=MAX_KEYS)
    )
    await create_schema(application)
    yield application
    await application.state.engine.dispose()


def client_from(app: FastAPI, address: str) -> AsyncClient:
    transport = ASGITransport(app=app, raise_app_exceptions=False, client=(address, 50_000))
    return AsyncClient(transport=transport, base_url="http://test")


async def login_from(
    app: FastAPI, address: str, email: str, password: str = WRONG_PASSWORD
) -> Response:
    async with client_from(app, address) as client:
        return await client.post("/api/v1/auth/login", json={"email": email, "password": password})


async def forgot_from(app: FastAPI, address: str, email: str) -> Response:
    async with client_from(app, address) as client:
        return await client.post("/api/v1/auth/forgot-password", json={"email": email})


async def register_victim(app: FastAPI) -> None:
    async with client_from(app, "192.0.2.1") as client:
        assert (await register_user(client)).status_code == 201


# ----------------------------------------------------------------------------- M-2: login
async def test_fake_email_flood_then_distributed_bruteforce_is_capped_per_account(
    app: FastAPI,
) -> None:
    await register_victim(app)
    for index in range(FAKE_EMAIL_FLOOD):  # 2 IP, mỗi IP nhiều email giả, trong giới hạn IP
        await login_from(app, peer(index % 2), f"fake{index}@attacker.example.net")

    attempts = [  # rồi dò tài khoản thật từ 10 IP khác, mỗi IP vẫn dưới giới hạn IP
        (await login_from(app, peer(10 + index % 10), DEFAULT_EMAIL)).status_code
        for index in range(LOGIN_PER_EMAIL.max_requests * 3)
    ]

    # Đúng ngân sách của tài khoản tới được bước kiểm tra mật khẩu, còn lại bị 429
    assert attempts.count(401) == LOGIN_PER_EMAIL.max_requests
    assert attempts.count(429) == LOGIN_PER_EMAIL.max_requests * 2


async def test_email_case_variants_share_one_account_budget(app: FastAPI) -> None:
    await register_victim(app)
    variants = [DEFAULT_EMAIL.upper(), DEFAULT_EMAIL.title(), f"  {DEFAULT_EMAIL}  "]

    statuses = [
        (await login_from(app, peer(index % 4), variants[index % len(variants)])).status_code
        for index in range(LOGIN_PER_EMAIL.max_requests + 1)
    ]

    assert statuses[-1] == 429


async def test_concurrent_distributed_attempts_never_exceed_account_budget(app: FastAPI) -> None:
    await register_victim(app)

    responses = await asyncio.gather(
        *(login_from(app, peer(index % 12), DEFAULT_EMAIL) for index in range(40))
    )

    statuses = [response.status_code for response in responses]
    assert statuses.count(401) == LOGIN_PER_EMAIL.max_requests
    assert statuses.count(429) == 40 - LOGIN_PER_EMAIL.max_requests


async def test_account_budget_is_shared_between_app_instances(tmp_path: Path) -> None:
    """Hai worker dùng chung database: ngân sách tài khoản không nhân theo số worker."""
    settings = build_settings(database_url=sqlite_file_url(tmp_path))
    worker_a, worker_b = create_app(settings), create_app(settings)
    await create_schema(worker_a)
    try:
        await register_victim(worker_a)
        statuses = [
            (await login_from(worker, peer(index), DEFAULT_EMAIL)).status_code
            for index, worker in enumerate([worker_a, worker_b] * LOGIN_PER_EMAIL.max_requests)
        ]
    finally:
        await worker_a.state.engine.dispose()
        await worker_b.state.engine.dispose()

    assert statuses.count(401) == LOGIN_PER_EMAIL.max_requests


async def test_unknown_and_existing_emails_get_identical_responses(app: FastAPI) -> None:
    await register_victim(app)

    async def sequence(email: str, offset: int) -> list[tuple[int, str, bool]]:
        result = []
        for index in range(LOGIN_PER_EMAIL.max_requests + 1):
            response = await login_from(app, peer(offset + index % 3), email)
            result.append(
                (
                    response.status_code,
                    response.json()["error"]["code"],
                    "retry-after" in response.headers,
                )
            )
        return result

    assert await sequence(DEFAULT_EMAIL, 0) == await sequence("nobody@example.com", 5)


# ----------------------------------------------------------------------------- L-1: thiết bị quen
async def test_attacker_cannot_lock_owner_out_of_a_known_device(app: FastAPI) -> None:
    await register_victim(app)
    async with client_from(app, "192.0.2.10") as owner:
        first = await owner.post(
            "/api/v1/auth/login", json={"email": DEFAULT_EMAIL, "password": DEFAULT_PASSWORD}
        )
        assert first.status_code == 200

        for index in range(LOGIN_PER_EMAIL.max_requests * 2):  # kẻ tấn công khóa tài khoản
            await login_from(app, peer(index % 5), DEFAULT_EMAIL)
        assert (
            await login_from(app, "192.0.2.99", DEFAULT_EMAIL, DEFAULT_PASSWORD)
        ).status_code == 429

        again = await owner.post(
            "/api/v1/auth/login", json={"email": DEFAULT_EMAIL, "password": DEFAULT_PASSWORD}
        )

    assert again.status_code == 200  # trình duyệt đã từng đăng nhập vẫn vào được


# ----------------------------------------------------------------------------- M-2: forgot
async def test_fake_email_flood_then_reset_spam_is_capped_per_account(app: FastAPI) -> None:
    await register_victim(app)
    for index in range(FAKE_EMAIL_FLOOD // 2):  # 2 IP x 10 email giả (giới hạn IP: 10/giờ)
        await forgot_from(app, peer(index % 2), f"fake{index}@attacker.example.net")

    for index in range(FORGOT_PASSWORD_PER_EMAIL.max_requests * 4):
        await forgot_from(app, peer(10 + index % 6), DEFAULT_EMAIL)

    sender = app.state.email_sender
    assert isinstance(sender, InMemoryEmailSender)
    to_victim = [message for message in sender.messages if message.to == DEFAULT_EMAIL]
    assert len(to_victim) == FORGOT_PASSWORD_PER_EMAIL.max_requests


async def test_forgot_password_responses_do_not_reveal_existing_accounts(app: FastAPI) -> None:
    await register_victim(app)

    async def sequence(email: str, offset: int) -> list[tuple[int, str]]:
        responses = [
            await forgot_from(app, peer(offset + index % 2), email)
            for index in range(FORGOT_PASSWORD_PER_EMAIL.max_requests + 1)
        ]
        return [(response.status_code, response.text) for response in responses]

    assert await sequence(DEFAULT_EMAIL, 0) == await sequence("nobody@example.com", 5)


# ----------------------------------------------------------------------------- L-10: Argon2
class ConcurrencyProbe:
    def __init__(self) -> None:
        self.active = 0
        self.peak = 0
        self._lock = threading.Lock()

    def __enter__(self) -> None:
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)

    def __exit__(self, *_: object) -> None:
        with self._lock:
            self.active -= 1


async def test_concurrent_logins_bound_parallel_argon2_work(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = ConcurrencyProbe()
    real_hasher = security._PASSWORD_HASHER

    class ProbedHasher(PasswordHasher):
        def verify(self, hash: str | bytes, password: str | bytes) -> Literal[True]:
            with probe:  # đo số lượt Argon2 thật đang chạy cùng lúc
                return real_hasher.verify(hash, password)

    monkeypatch.setattr(security, "_PASSWORD_HASHER", ProbedHasher())

    await asyncio.gather(
        *(login_from(app, peer(index % 12), f"u{index}@example.com") for index in range(24))
    )

    assert 1 <= probe.peak <= ARGON2_PARALLEL_LIMIT


# ----------------------------------------------------------------------------- lỗi database
@pytest.mark.parametrize("endpoint", ["login", "forgot-password"])
async def test_throttle_storage_failure_fails_closed(app: FastAPI, endpoint: str) -> None:
    await register_victim(app)
    async with app.state.engine.begin() as connection:
        await connection.execute(text("DROP TABLE auth_throttles"))  # tên bảng cố định trong test

    async with client_from(app, "192.0.2.50") as client:
        response = await client.post(
            f"/api/v1/auth/{endpoint}",
            json={"email": DEFAULT_EMAIL, "password": DEFAULT_PASSWORD}
            if endpoint == "login"
            else {"email": DEFAULT_EMAIL},
        )

    assert response.status_code == 500  # không có bộ đếm → không cho đăng nhập / gửi mail
    assert "set-cookie" not in response.headers
    assert app.state.email_sender.messages == []
