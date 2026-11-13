"""Lỗi database ngoài dự kiến không được làm lộ tham số SQL (email, hash, token) vào log."""

import logging
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.main import create_app
from tests.conftest import build_settings, create_schema, sqlite_file_url
from tests.helpers import DEFAULT_EMAIL, DEFAULT_FULL_NAME, DEFAULT_PASSWORD, register_user

pytestmark = pytest.mark.integration

HIDDEN_MARKER = "SQL parameters hidden"
HEX_DIGEST = re.compile(r"\b[0-9a-f]{64}\b")  # dạng hash SHA-256 của token phiên / token reset


def assert_log_has_no_sql_parameters(log_text: str) -> None:
    assert HIDDEN_MARKER in log_text, "phải có lỗi DB thật được ghi log (và đã che tham số)"
    assert "[parameters:" not in log_text
    assert DEFAULT_EMAIL not in log_text
    assert DEFAULT_FULL_NAME not in log_text
    assert DEFAULT_PASSWORD not in log_text
    assert "$argon2id$" not in log_text
    assert HEX_DIGEST.search(log_text) is None


async def drop_table(app: FastAPI, table: str) -> None:
    async with app.state.engine.begin() as connection:
        await connection.execute(text(f"DROP TABLE {table}"))  # tên bảng cố định trong test


@pytest.fixture
async def broken_app(tmp_path: Path) -> FastAPI:
    app = create_app(build_settings(database_url=sqlite_file_url(tmp_path)))
    await create_schema(app)
    return app


async def test_register_db_failure_does_not_log_email_or_password_hash(
    broken_app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    await drop_table(broken_app, "users")
    transport = ASGITransport(app=broken_app, raise_app_exceptions=False)

    with caplog.at_level(logging.ERROR):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await register_user(client)
    await broken_app.state.engine.dispose()

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert_log_has_no_sql_parameters(caplog.text)


async def test_login_db_failure_does_not_log_session_token_hash(
    broken_app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    transport = ASGITransport(app=broken_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await register_user(client)).status_code == 201
        await drop_table(broken_app, "user_sessions")  # INSERT phiên sẽ lỗi

        with caplog.at_level(logging.ERROR):
            response = await client.post(
                "/api/v1/auth/login", json={"email": DEFAULT_EMAIL, "password": DEFAULT_PASSWORD}
            )
    await broken_app.state.engine.dispose()

    assert response.status_code == 500
    assert_log_has_no_sql_parameters(caplog.text)


async def test_forgot_password_db_failure_does_not_log_reset_token_hash(
    broken_app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    transport = ASGITransport(app=broken_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await register_user(client)).status_code == 201
        await drop_table(broken_app, "password_reset_tokens")

        with caplog.at_level(logging.ERROR):
            response = await client.post(
                "/api/v1/auth/forgot-password", json={"email": DEFAULT_EMAIL}
            )
    await broken_app.state.engine.dispose()

    # Token được tạo ở tác vụ nền sau khi trả lời → phản hồi giống hệt trường hợp thành công (không
    # lộ email tồn tại qua status/thời gian); lỗi DB vẫn được ghi log, đã che tham số SQL
    assert response.status_code == 202
    assert "Failed to issue password reset token" in caplog.text
    assert_log_has_no_sql_parameters(caplog.text)
