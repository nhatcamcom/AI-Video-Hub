import asyncio
import logging
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.main import create_app
from app.modules.auth.models import PasswordResetToken
from app.modules.auth.security import hash_token
from tests.conftest import build_production_settings, create_schema, sqlite_file_url
from tests.helpers import (
    DEFAULT_EMAIL,
    DEFAULT_PASSWORD,
    extract_reset_token,
    login_user,
    outbox,
    register_and_login,
    register_user,
)

pytestmark = pytest.mark.integration

NEW_PASSWORD = "brand-new-passphrase-2026"


async def request_reset(client: AsyncClient, email: str = DEFAULT_EMAIL) -> int:
    response = await client.post("/api/v1/auth/forgot-password", json={"email": email})
    return response.status_code


async def reset(client: AsyncClient, token: str, new_password: str = NEW_PASSWORD) -> int:
    response = await client.post(
        "/api/v1/auth/reset-password", json={"token": token, "new_password": new_password}
    )
    return response.status_code


# ----------------------------------------------------------------------------- forgot-password
async def test_forgot_password_sends_reset_email_for_existing_user(
    app: FastAPI, client: AsyncClient
) -> None:
    await register_user(client)

    assert await request_reset(client) == 202

    messages = outbox(app).messages
    assert len(messages) == 1
    assert messages[0].to == DEFAULT_EMAIL
    assert "/reset-password#token=" in messages[0].body  # token nằm sau '#', không vào log server


async def test_forgot_password_does_not_reveal_whether_email_exists(
    app: FastAPI, client: AsyncClient
) -> None:
    await register_user(client)

    known = await client.post("/api/v1/auth/forgot-password", json={"email": DEFAULT_EMAIL})
    unknown = await client.post("/api/v1/auth/forgot-password", json={"email": "ghost@example.com"})

    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()
    assert len(outbox(app).messages) == 1  # chỉ email thật mới nhận thư


async def test_forgot_password_response_never_contains_the_token(
    app: FastAPI, client: AsyncClient
) -> None:
    await register_user(client)

    response = await client.post("/api/v1/auth/forgot-password", json={"email": DEFAULT_EMAIL})

    assert extract_reset_token(app) not in response.text


async def test_forgot_password_stores_only_token_hash(app: FastAPI, client: AsyncClient) -> None:
    await register_user(client)
    await request_reset(client)
    token = extract_reset_token(app)

    async with app.state.session_factory() as session:
        stored = (await session.execute(select(PasswordResetToken))).scalar_one()

    assert stored.token_hash == hash_token(token)
    assert stored.used_at is None
    assert stored.expires_at - stored.created_at == timedelta(minutes=30)


async def test_forgot_password_in_production_returns_no_token(tmp_path: Path) -> None:
    app = create_app(build_production_settings(database_url=sqlite_file_url(tmp_path)))
    await create_schema(app)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with AsyncClient(transport=transport, base_url="https://test") as client:
            await register_user(client)
            response = await client.post(
                "/api/v1/auth/forgot-password", json={"email": DEFAULT_EMAIL}
            )
    finally:
        await app.state.engine.dispose()

    assert response.status_code == 202
    assert set(response.json()) == {"message"}
    assert "token" not in response.text.lower()


async def test_forgot_password_rejects_invalid_email(client: AsyncClient) -> None:
    response = await client.post("/api/v1/auth/forgot-password", json={"email": "nope"})

    assert response.status_code == 422


# ----------------------------------------------------------------------------- reset-password
async def test_reset_password_changes_password(app: FastAPI, client: AsyncClient) -> None:
    await register_user(client)
    await request_reset(client)

    assert await reset(client, extract_reset_token(app)) == 200

    assert (await login_user(client, password=DEFAULT_PASSWORD)).status_code == 401
    assert (await login_user(client, password=NEW_PASSWORD)).status_code == 200


async def test_reset_token_cannot_be_used_twice(app: FastAPI, client: AsyncClient) -> None:
    await register_user(client)
    await request_reset(client)
    token = extract_reset_token(app)
    assert await reset(client, token) == 200

    response = await client.post(
        "/api/v1/auth/reset-password",
        json={"token": token, "new_password": "attacker-chosen-pass-1"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_reset_token"
    assert (await login_user(client, password=NEW_PASSWORD)).status_code == 200


async def test_expired_reset_token_is_rejected(app: FastAPI, client: AsyncClient) -> None:
    await register_user(client)
    await request_reset(client)
    token = extract_reset_token(app)
    async with app.state.session_factory() as session:
        stored = (await session.execute(select(PasswordResetToken))).scalar_one()
        stored.expires_at = stored.created_at - timedelta(seconds=1)
        await session.commit()

    assert await reset(client, token) == 400
    assert (await login_user(client, password=DEFAULT_PASSWORD)).status_code == 200


async def test_unknown_reset_token_is_rejected(client: AsyncClient) -> None:
    assert await reset(client, "a" * 43) == 400


async def test_concurrent_resets_with_the_same_token_succeed_only_once(
    app: FastAPI, client: AsyncClient
) -> None:
    await register_user(client)
    await request_reset(client)
    token = extract_reset_token(app)
    candidates = [f"concurrent-passphrase-{index:02d}" for index in range(5)]

    statuses = await asyncio.gather(*(reset(client, token, password) for password in candidates))

    assert sorted(statuses) == [200, 400, 400, 400, 400]
    winner = candidates[statuses.index(200)]
    for password in candidates:
        expected = 200 if password == winner else 401
        assert (await login_user(client, password=password)).status_code == expected


async def test_requesting_new_reset_invalidates_previous_token(
    app: FastAPI, client: AsyncClient
) -> None:
    await register_user(client)
    await request_reset(client)
    first_token = extract_reset_token(app)
    await request_reset(client)

    assert await reset(client, first_token) == 400
    assert await reset(client, extract_reset_token(app)) == 200


async def test_reset_password_enforces_password_policy(app: FastAPI, client: AsyncClient) -> None:
    await register_user(client)
    await request_reset(client)

    assert await reset(client, extract_reset_token(app), new_password="short") == 422


async def test_reset_password_revokes_existing_sessions(app: FastAPI, client: AsyncClient) -> None:
    await register_and_login(client)
    assert (await client.get("/api/v1/auth/me")).status_code == 200
    await request_reset(client)

    assert await reset(client, extract_reset_token(app)) == 200

    assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_auth_flows_never_log_passwords_or_tokens(
    app: FastAPI, client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        await register_and_login(client)
        await request_reset(client)
        token = extract_reset_token(app)
        await reset(client, token)
        await login_user(client, password=NEW_PASSWORD)

    for secret in (DEFAULT_PASSWORD, NEW_PASSWORD, token):
        assert secret not in caplog.text
