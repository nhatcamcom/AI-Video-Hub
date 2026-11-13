"""Xác thực (ai đang gọi?) và phân quyền (được làm gì?).

CSRF được kiểm tra NGAY trong dependency xác thực cho mọi method thay đổi trạng thái, nên
mọi endpoint dùng `CurrentUserDep`/`AdminUserDep` mặc định đã được bảo vệ — không thể quên.
"""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request

from app.core.config import Settings
from app.core.database import DbDep
from app.core.dependencies import SettingsDep
from app.core.errors import ForbiddenError, UnauthorizedError
from app.modules.auth.errors import CsrfValidationError
from app.modules.auth.models import UserSession
from app.modules.auth.security import token_matches_hash
from app.modules.auth.service import AuthService
from app.modules.users.models import User, UserRole

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class AuthContext:
    user: User
    session: UserSession


def get_auth_service(db: DbDep, settings: SettingsDep) -> AuthService:
    return AuthService(db, settings)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]


def _verify_csrf(request: Request, settings: Settings, session: UserSession) -> None:
    submitted = request.headers.get(settings.csrf_header_name)
    if not submitted or not token_matches_hash(submitted, session.csrf_token_hash):
        raise CsrfValidationError


async def get_optional_auth(
    request: Request, service: AuthServiceDep, settings: SettingsDep
) -> AuthContext | None:
    token = request.cookies.get(settings.session_cookie_key)
    if not token:
        return None
    session = await service.resolve_session(token)
    if session is None:
        return None
    if request.method not in SAFE_METHODS:
        _verify_csrf(request, settings, session)
    return AuthContext(user=session.user, session=session)


OptionalAuthDep = Annotated[AuthContext | None, Depends(get_optional_auth)]


async def get_auth(context: OptionalAuthDep) -> AuthContext:
    if context is None:
        raise UnauthorizedError
    return context


AuthDep = Annotated[AuthContext, Depends(get_auth)]


async def get_current_user(context: AuthDep) -> User:
    return context.user


CurrentUserDep = Annotated[User, Depends(get_current_user)]


async def require_admin(user: CurrentUserDep) -> User:
    if user.role is not UserRole.ADMIN:
        raise ForbiddenError
    return user


AdminUserDep = Annotated[User, Depends(require_admin)]
