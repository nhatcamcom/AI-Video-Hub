"""L-9: dòng log audit chỉ được ghi SAU khi transaction đổi quyền đã commit thành công."""

import logging

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.modules.audit.models import AuditEvent
from app.modules.audit.service import ADMIN_ROLE_GRANTED
from app.modules.users.models import User, UserRole
from app.modules.users.service import UserService
from tests.helpers import DEFAULT_EMAIL, register_user

pytestmark = pytest.mark.integration


async def test_failed_commit_writes_no_audit_log_line(
    app: FastAPI,
    client: AsyncClient,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await register_user(client)

    async with app.state.session_factory() as session:

        async def failing_commit() -> None:
            raise OperationalError("COMMIT", {}, Exception("disk I/O error"))

        monkeypatch.setattr(session, "commit", failing_commit)
        with caplog.at_level(logging.INFO), pytest.raises(OperationalError):
            await UserService(session).promote_to_admin(DEFAULT_EMAIL, actor="cli:test@host")

    assert ADMIN_ROLE_GRANTED not in caplog.text  # không có dấu vết cho thay đổi đã rollback
    async with app.state.session_factory() as session:
        role = (await session.execute(select(User.role))).scalar_one()
        events = list((await session.execute(select(AuditEvent))).scalars())
    assert (role, events) == (UserRole.USER, [])


async def test_successful_promotion_logs_after_commit(
    app: FastAPI, client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    await register_user(client)

    async with app.state.session_factory() as session:
        with caplog.at_level(logging.INFO):
            await UserService(session).promote_to_admin(DEFAULT_EMAIL, actor="cli:test@host")

    assert ADMIN_ROLE_GRANTED in caplog.text
    assert DEFAULT_EMAIL not in caplog.text
