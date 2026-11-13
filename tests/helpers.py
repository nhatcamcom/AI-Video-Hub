"""Thao tác lặp lại trong test auth — đi qua API thật, không giả lập xác thực."""

import re

from fastapi import FastAPI
from httpx import AsyncClient, Response

from app.core.email import InMemoryEmailSender
from app.modules.users.service import UserService

DEFAULT_PASSWORD = "correct-horse-battery-staple"
DEFAULT_EMAIL = "alice@example.com"
DEFAULT_FULL_NAME = "Alice Nguyễn"
SESSION_COOKIE = "avh_session"
CSRF_COOKIE = "avh_csrf"
CSRF_HEADER = "X-CSRF-Token"

RESET_TOKEN_PATTERN = re.compile(r"#token=([A-Za-z0-9_\-]+)")


async def register_user(
    client: AsyncClient,
    email: str = DEFAULT_EMAIL,
    password: str = DEFAULT_PASSWORD,
    full_name: str = DEFAULT_FULL_NAME,
) -> Response:
    return await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "full_name": full_name},
    )


async def login_user(
    client: AsyncClient, email: str = DEFAULT_EMAIL, password: str = DEFAULT_PASSWORD
) -> Response:
    return await client.post("/api/v1/auth/login", json={"email": email, "password": password})


async def register_and_login(
    client: AsyncClient, email: str = DEFAULT_EMAIL, password: str = DEFAULT_PASSWORD
) -> None:
    assert (await register_user(client, email=email, password=password)).status_code == 201
    assert (await login_user(client, email=email, password=password)).status_code == 200


def csrf_headers(client: AsyncClient) -> dict[str, str]:
    return {CSRF_HEADER: client.cookies[CSRF_COOKIE]}


async def promote_to_admin(app: FastAPI, email: str) -> None:
    """Cấp quyền ADMIN bằng đúng service mà CLI `promote-admin` sử dụng."""
    async with app.state.session_factory() as session:
        await UserService(session).promote_to_admin(email, actor="test-suite")


def outbox(app: FastAPI) -> InMemoryEmailSender:
    sender = app.state.email_sender
    assert isinstance(sender, InMemoryEmailSender)
    return sender


def extract_reset_token(app: FastAPI) -> str:
    message = outbox(app).messages[-1]
    match = RESET_TOKEN_PATTERN.search(message.body)
    assert match is not None, "email reset không chứa token"
    return match.group(1)
