"""Các nhánh bảo mật ít gặp: nâng cấp hash, tài khoản bị khóa, lỗi gửi email."""

import logging

import pytest
from argon2 import PasswordHasher
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select, update

from app.core.email import EmailMessage
from app.modules.users.models import User
from tests.helpers import (
    DEFAULT_EMAIL,
    DEFAULT_PASSWORD,
    extract_reset_token,
    login_user,
    register_user,
)

pytestmark = pytest.mark.integration


async def test_login_upgrades_outdated_password_hash(app: FastAPI, client: AsyncClient) -> None:
    await register_user(client)
    weak_hash = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash(DEFAULT_PASSWORD)
    async with app.state.session_factory() as session:
        await session.execute(update(User).values(password_hash=weak_hash))
        await session.commit()

    assert (await login_user(client)).status_code == 200

    async with app.state.session_factory() as session:
        upgraded = (await session.execute(select(User.password_hash))).scalar_one()
    assert upgraded != weak_hash
    assert "m=65536" in upgraded  # tham số hiện hành
    assert (await login_user(client)).status_code == 200  # hash mới vẫn đúng mật khẩu


async def test_deactivated_account_cannot_reset_password(app: FastAPI, client: AsyncClient) -> None:
    await register_user(client)
    await client.post("/api/v1/auth/forgot-password", json={"email": DEFAULT_EMAIL})
    token = extract_reset_token(app)
    async with app.state.session_factory() as session:
        await session.execute(update(User).values(is_active=False))
        await session.commit()

    response = await client.post(
        "/api/v1/auth/reset-password",
        json={"token": token, "new_password": "brand-new-passphrase-2026"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_reset_token"


async def test_forgot_password_for_deactivated_account_sends_nothing(
    app: FastAPI, client: AsyncClient
) -> None:
    await register_user(client)
    async with app.state.session_factory() as session:
        await session.execute(update(User).values(is_active=False))
        await session.commit()

    response = await client.post("/api/v1/auth/forgot-password", json={"email": DEFAULT_EMAIL})

    assert response.status_code == 202
    assert app.state.email_sender.messages == []


class ExplodingEmailSender:
    async def send(self, message: EmailMessage) -> None:
        raise ConnectionError("smtp down")


async def test_email_failure_does_not_change_response_or_leak_token(
    app: FastAPI, client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    await register_user(client)
    app.state.email_sender = ExplodingEmailSender()

    with caplog.at_level(logging.ERROR):
        response = await client.post("/api/v1/auth/forgot-password", json={"email": DEFAULT_EMAIL})

    assert response.status_code == 202  # giống hệt trường hợp thành công
    assert "Failed to send email" in caplog.text
    assert "#token=" not in caplog.text
