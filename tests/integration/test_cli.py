"""CLI quản trị: cách duy nhất để cấp quyền ADMIN (không có API tự nâng quyền)."""

import asyncio
import logging
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, update

from app.cli import (
    EXIT_CONFIRMATION_REQUIRED,
    EXIT_FAILED,
    EXIT_OK,
    current_operator,
    describe_database,
    main,
    run_cli,
)
from app.core.config import Settings, get_settings
from app.core.database import Base, create_engine, create_session_factory
from app.core.time import utc_now
from app.modules.audit.models import AuditEvent
from app.modules.audit.service import ADMIN_ROLE_GRANTED
from app.modules.auth.security import hash_password
from app.modules.users.models import User, UserRole
from app.modules.users.service import UserService
from tests.conftest import build_settings, sqlite_file_url

pytestmark = pytest.mark.integration

ACTOR = "cli:tester@test-host"


def prepare_database(settings: Settings, email: str | None, *, is_active: bool = True) -> None:
    async def setup() -> None:
        engine = create_engine(settings)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        if email is not None:
            async with create_session_factory(engine)() as session:
                user = await UserService(session).create(
                    email=email, password_hash=hash_password("x" * 12), full_name="Bob"
                )
                if not is_active:
                    await session.execute(update(User).values(is_active=False))
                    await session.commit()
                assert user.id
        await engine.dispose()

    asyncio.run(setup())


def read_state(settings: Settings, email: str) -> tuple[UserRole, list[AuditEvent]]:
    async def query() -> tuple[UserRole, list[AuditEvent]]:
        engine = create_engine(settings)
        async with create_session_factory(engine)() as session:
            role = (
                await session.execute(select(User.role).where(User.email == email))
            ).scalar_one()
            events = list((await session.execute(select(AuditEvent))).scalars())
        await engine.dispose()
        return role, events

    return asyncio.run(query())


def promote(
    settings: Settings,
    email: str,
    *extra: str,
    answer: str | None = None,
    is_interactive: bool = False,
) -> int:
    return run_cli(
        ["promote-admin", email, *extra],
        settings,
        prompt=lambda _: answer or "",
        is_interactive=lambda: is_interactive,
        actor=ACTOR,
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return build_settings(database_url=sqlite_file_url(tmp_path))


# ----------------------------------------------------------------------------- thành công
def test_promote_admin_with_yes_grants_role_and_records_audit_event(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    prepare_database(settings, email="bob@example.com")
    before = utc_now()

    exit_code = promote(settings, "BOB@example.com", "--yes")

    assert exit_code == EXIT_OK
    role, events = read_state(settings, "bob@example.com")
    assert role is UserRole.ADMIN
    assert len(events) == 1
    event = events[0]
    assert (event.actor, event.action) == (ACTOR, ADMIN_ROLE_GRANTED)
    assert event.target_user_id is not None
    assert before - timedelta(seconds=1) <= event.occurred_at <= utc_now()
    assert "bob@example.com" in capsys.readouterr().out


def test_output_shows_target_database_before_acting(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    prepare_database(settings, email="bob@example.com")

    promote(settings, "bob@example.com", "--yes")

    output = capsys.readouterr().out
    assert "Database đích" in output
    assert "test.db" in output


def test_describe_database_hides_password() -> None:
    described = describe_database("postgresql+asyncpg://app:S3cr3t-pass@db.internal:5432/avh")

    assert "S3cr3t-pass" not in described
    assert "db.internal" in described
    assert "avh" in described


def test_current_operator_identifies_os_user_and_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.cli.getpass.getuser", lambda: "alice")
    monkeypatch.setattr("app.cli.socket.gethostname", lambda: "ops-box")

    assert current_operator() == "cli:alice@ops-box"


def test_current_operator_falls_back_when_os_user_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_user() -> str:
        raise OSError("no login name")

    monkeypatch.setattr("app.cli.getpass.getuser", no_user)
    monkeypatch.setattr("app.cli.socket.gethostname", lambda: "ops-box")

    assert current_operator() == "cli:unknown@ops-box"


def test_describe_database_handles_unparseable_url() -> None:
    assert "không đọc được" in describe_database("::not a url::")


# ----------------------------------------------------------------------------- xác nhận
def test_promote_admin_requires_yes_when_not_interactive(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    prepare_database(settings, email="bob@example.com")

    exit_code = promote(settings, "bob@example.com", is_interactive=False)

    assert exit_code == EXIT_CONFIRMATION_REQUIRED
    assert "--yes" in capsys.readouterr().err
    role, events = read_state(settings, "bob@example.com")
    assert role is UserRole.USER
    assert events == []


def test_interactive_confirmation_yes_grants_role(settings: Settings) -> None:
    prepare_database(settings, email="bob@example.com")

    exit_code = promote(settings, "bob@example.com", answer="yes", is_interactive=True)

    assert exit_code == EXIT_OK
    assert read_state(settings, "bob@example.com")[0] is UserRole.ADMIN


@pytest.mark.parametrize("answer", ["", "y", "no", "YES please"])
def test_interactive_confirmation_other_answers_abort(settings: Settings, answer: str) -> None:
    prepare_database(settings, email="bob@example.com")

    exit_code = promote(settings, "bob@example.com", answer=answer, is_interactive=True)

    assert exit_code == EXIT_FAILED
    role, events = read_state(settings, "bob@example.com")
    assert role is UserRole.USER
    assert events == []


def test_end_of_input_is_treated_as_missing_confirmation(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """Trên Windows, stdin = NUL báo isatty()=True nhưng input() gặp EOF ngay."""
    prepare_database(settings, email="bob@example.com")

    def end_of_input(_: str) -> str:
        raise EOFError

    exit_code = run_cli(
        ["promote-admin", "bob@example.com"],
        settings,
        prompt=end_of_input,
        is_interactive=lambda: True,
        actor=ACTOR,
    )

    assert exit_code == EXIT_CONFIRMATION_REQUIRED
    assert "--yes" in capsys.readouterr().err
    assert read_state(settings, "bob@example.com") == (UserRole.USER, [])


def test_ctrl_c_at_confirmation_aborts(settings: Settings) -> None:
    prepare_database(settings, email="bob@example.com")

    def interrupted(_: str) -> str:
        raise KeyboardInterrupt

    exit_code = run_cli(
        ["promote-admin", "bob@example.com"],
        settings,
        prompt=interrupted,
        is_interactive=lambda: True,
        actor=ACTOR,
    )

    assert exit_code == EXIT_FAILED
    assert read_state(settings, "bob@example.com") == (UserRole.USER, [])


# ----------------------------------------------------------------------------- trường hợp đặc biệt
def test_already_admin_reports_status_without_new_audit_event(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    prepare_database(settings, email="bob@example.com")
    promote(settings, "bob@example.com", "--yes")
    capsys.readouterr()

    exit_code = promote(settings, "bob@example.com", "--yes")

    assert exit_code == EXIT_OK
    assert "đã là ADMIN" in capsys.readouterr().out
    assert len(read_state(settings, "bob@example.com")[1]) == 1


def test_inactive_account_is_refused(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    prepare_database(settings, email="bob@example.com", is_active=False)

    exit_code = promote(settings, "bob@example.com", "--yes")

    assert exit_code == EXIT_FAILED
    assert "bị khóa" in capsys.readouterr().err
    role, events = read_state(settings, "bob@example.com")
    assert role is UserRole.USER
    assert events == []


def test_promote_admin_fails_for_unknown_email(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    prepare_database(settings, email=None)

    exit_code = promote(settings, "ghost@example.com", "--yes")

    assert exit_code == EXIT_FAILED
    assert "không tồn tại" in capsys.readouterr().err


def test_audit_log_line_does_not_contain_email(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    prepare_database(settings, email="bob@example.com")

    with caplog.at_level(logging.INFO):
        promote(settings, "bob@example.com", "--yes")

    assert ADMIN_ROLE_GRANTED in caplog.text
    assert ACTOR in caplog.text
    assert "bob@example.com" not in caplog.text


def test_main_reads_settings_from_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = build_settings(database_url=sqlite_file_url(tmp_path))
    prepare_database(settings, email="carol@example.com")
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    monkeypatch.setenv("EMAIL_BACKEND", "memory")
    monkeypatch.setattr("sys.argv", ["app.cli", "promote-admin", "carol@example.com", "--yes"])
    get_settings.cache_clear()
    try:
        exit_code = main()
    finally:
        get_settings.cache_clear()

    assert exit_code == EXIT_OK
    assert read_state(settings, "carol@example.com")[0] is UserRole.ADMIN
