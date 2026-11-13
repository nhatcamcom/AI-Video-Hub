"""Dọn session / token reset đã hết hạn hoặc đã thu hồi, quá thời gian lưu giữ."""

import asyncio
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.cli import EXIT_OK, run_cli
from app.core.time import utc_now
from app.modules.auth.models import PasswordResetToken, UserSession
from app.modules.auth.security import generate_token, hash_token
from app.modules.auth.service import AUTH_RECORD_RETENTION, AuthService
from app.modules.users.models import User

pytestmark = pytest.mark.integration

LONG_AGO = AUTH_RECORD_RETENTION + timedelta(days=1)
RECENTLY = timedelta(hours=1)


def session_row(
    user_id: int, *, expires_at: datetime, revoked_at: datetime | None = None
) -> UserSession:
    return UserSession(
        user_id=user_id,
        token_hash=hash_token(generate_token()),
        csrf_token_hash=hash_token(generate_token()),
        created_at=expires_at - timedelta(days=7),
        expires_at=expires_at,
        revoked_at=revoked_at,
    )


def reset_row(user_id: int, *, expires_at: datetime) -> PasswordResetToken:
    return PasswordResetToken(
        user_id=user_id,
        token_hash=hash_token(generate_token()),
        created_at=expires_at - timedelta(minutes=30),
        expires_at=expires_at,
    )


async def seed(app: FastAPI) -> dict[str, int]:
    now = utc_now()
    async with app.state.session_factory() as session:
        user = User(email="bob@example.com", password_hash="x", full_name="Bob")
        session.add(user)
        await session.flush()
        rows = {
            "active": session_row(user.id, expires_at=now + timedelta(days=1)),
            "recently_expired": session_row(user.id, expires_at=now - RECENTLY),
            "long_expired": session_row(user.id, expires_at=now - LONG_AGO),
            "recently_revoked": session_row(
                user.id, expires_at=now + timedelta(days=1), revoked_at=now - RECENTLY
            ),
            "long_revoked": session_row(
                user.id, expires_at=now + timedelta(days=1), revoked_at=now - LONG_AGO
            ),
        }
        tokens = {
            "recent_token": reset_row(user.id, expires_at=now - RECENTLY),
            "old_token": reset_row(user.id, expires_at=now - LONG_AGO),
        }
        session.add_all([*rows.values(), *tokens.values()])
        await session.commit()
        session_ids = {name: row.id for name, row in rows.items()}
        token_ids = {name: row.id for name, row in tokens.items()}
        return {**session_ids, **token_ids}


async def remaining_ids(app: FastAPI) -> tuple[set[int], set[int]]:
    async with app.state.session_factory() as session:
        sessions = set((await session.execute(select(UserSession.id))).scalars())
        tokens = set((await session.execute(select(PasswordResetToken.id))).scalars())
    return sessions, tokens


async def test_purge_removes_only_records_past_retention(app: FastAPI) -> None:
    ids = await seed(app)

    async with app.state.session_factory() as session:
        result = await AuthService(session, app.state.settings).purge_expired_records()

    assert (result.sessions_deleted, result.reset_tokens_deleted) == (2, 1)
    sessions, tokens = await remaining_ids(app)
    assert sessions == {ids["active"], ids["recently_expired"], ids["recently_revoked"]}
    assert tokens == {ids["recent_token"]}


async def test_purge_is_idempotent(app: FastAPI) -> None:
    await seed(app)
    async with app.state.session_factory() as session:
        await AuthService(session, app.state.settings).purge_expired_records()

    async with app.state.session_factory() as session:
        again = await AuthService(session, app.state.settings).purge_expired_records()

    assert (again.sessions_deleted, again.reset_tokens_deleted) == (0, 0)


async def test_cleanup_cli_reports_counts(app: FastAPI, capsys: pytest.CaptureFixture[str]) -> None:
    await seed(app)

    # CLI tự gọi asyncio.run → chạy trong thread riêng như khi gọi từ shell (không lồng event loop)
    exit_code = await asyncio.to_thread(run_cli, ["cleanup-auth"], app.state.settings)

    assert exit_code == EXIT_OK
    output = capsys.readouterr().out
    assert "Database đích" in output
    assert "2 session" in output
    assert "1 token" in output
