"""CLI quản trị.

    python -m app.cli promote-admin <email> [--yes]
    python -m app.cli cleanup-auth

Đây là cách DUY NHẤT để cấp quyền ADMIN — không có API nào cho phép tự nâng quyền.
Mỗi lần cấp quyền được ghi vào bảng audit_events (ai, cho tài khoản nào, lúc nào).
"""

import argparse
import asyncio
import getpass
import socket
import sys
from collections.abc import Callable, Sequence

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from app.core.config import Settings, get_settings
from app.core.database import create_engine, create_session_factory
from app.core.errors import NotFoundError
from app.modules.auth.service import AuthService, PurgeResult
from app.modules.users.errors import AccountInactiveError
from app.modules.users.schemas import normalize_email
from app.modules.users.service import PromotionResult, UserService

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIRMATION_REQUIRED = 2
CONFIRMATION_WORD = "yes"

Prompt = Callable[[str], str]
InteractiveCheck = Callable[[], bool]


def describe_database(database_url: str) -> str:
    """URL database để hiển thị cho người vận hành, đã che mật khẩu."""
    try:
        return make_url(database_url).render_as_string(hide_password=True)
    except ArgumentError:
        return "<không đọc được DATABASE_URL>"


def current_operator() -> str:
    """Danh tính người chạy lệnh, ghi vào audit (tài khoản hệ điều hành + tên máy)."""
    try:
        user = getpass.getuser()
    except OSError:
        user = "unknown"
    return f"cli:{user}@{socket.gethostname()}"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Quản trị AI Video Hub")
    commands = parser.add_subparsers(dest="command", required=True)
    promote = commands.add_parser("promote-admin", help="Cấp quyền ADMIN cho tài khoản đã có")
    promote.add_argument("email", help="Email của tài khoản cần cấp quyền")
    promote.add_argument(
        "--yes", action="store_true", help="Bỏ bước xác nhận (dùng trong script không tương tác)"
    )
    commands.add_parser(
        "cleanup-auth",
        help="Xóa session/token reset đã hết hạn hoặc thu hồi quá thời gian lưu giữ (chạy định kỳ)",
    )
    return parser


async def _promote_admin(settings: Settings, email: str, actor: str) -> PromotionResult:
    engine = create_engine(settings)
    try:
        async with create_session_factory(engine)() as session:
            return await UserService(session).promote_to_admin(email, actor=actor)
    finally:
        await engine.dispose()


async def _purge_auth_records(settings: Settings) -> PurgeResult:
    engine = create_engine(settings)
    try:
        async with create_session_factory(engine)() as session:
            return await AuthService(session, settings).purge_expired_records()
    finally:
        await engine.dispose()


def _confirmed(assume_yes: bool, prompt: Prompt, is_interactive: InteractiveCheck) -> bool | None:
    """True/False theo câu trả lời; None nếu không thể hỏi (không có terminal) và thiếu --yes."""
    if assume_yes:
        return True
    if not is_interactive():
        return None
    try:
        answer = prompt(f"Gõ '{CONFIRMATION_WORD}' để cấp quyền ADMIN: ")
    except EOFError:
        # Windows: stdin = NUL vẫn báo isatty()=True nhưng không đọc được gì → coi như không hỏi
        return None
    except KeyboardInterrupt:
        return False  # người vận hành bấm Ctrl+C → hủy
    return answer.strip().lower() == CONFIRMATION_WORD


def run_cli(
    argv: Sequence[str],
    settings: Settings,
    *,
    prompt: Prompt = input,
    is_interactive: InteractiveCheck = sys.stdin.isatty,
    actor: str | None = None,
) -> int:
    args = _build_parser().parse_args(argv)
    print(f"Database đích: {describe_database(settings.database_url)}")
    if args.command == "cleanup-auth":
        return _run_cleanup(settings)
    return _run_promote(args, settings, prompt, is_interactive, actor)


def _run_cleanup(settings: Settings) -> int:
    result = asyncio.run(_purge_auth_records(settings))
    print(
        f"Đã xóa {result.sessions_deleted} session, {result.reset_tokens_deleted} token "
        f"đặt lại mật khẩu, {result.trusted_devices_deleted} thiết bị quen đã hết hạn và "
        f"{result.throttles_deleted} bộ đếm đăng nhập đã hồi."
    )
    return EXIT_OK


def _run_promote(
    args: argparse.Namespace,
    settings: Settings,
    prompt: Prompt,
    is_interactive: InteractiveCheck,
    actor: str | None,
) -> int:
    email = normalize_email(args.email)
    print(f"Tài khoản:     {email}")

    confirmed = _confirmed(args.yes, prompt, is_interactive)
    if confirmed is None:
        print("Từ chối: cần xác nhận. Chạy lại kèm --yes khi không có terminal.", file=sys.stderr)
        return EXIT_CONFIRMATION_REQUIRED
    if not confirmed:
        print("Đã hủy, không thay đổi gì.")
        return EXIT_FAILED

    try:
        result = asyncio.run(_promote_admin(settings, email, actor or current_operator()))
    except (NotFoundError, AccountInactiveError) as exc:
        print(f"Lỗi: {exc}", file=sys.stderr)
        return EXIT_FAILED

    if result.was_already_admin:
        print(f"{result.user.email} đã là ADMIN — không thay đổi.")
    else:
        print(f"Đã cấp quyền ADMIN cho {result.user.email}")
    return EXIT_OK


def main() -> int:
    # Console Windows (cp1258…) không in được mọi ký tự tiếng Việt → thay ký tự lỗi thay vì crash
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")
    return run_cli(sys.argv[1:], get_settings())


if __name__ == "__main__":
    raise SystemExit(main())
