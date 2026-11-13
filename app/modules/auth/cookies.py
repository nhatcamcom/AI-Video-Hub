"""Đặt / xóa cookie phiên.

SameSite=Lax (không phải Strict): người dùng mở link từ email vẫn giữ đăng nhập; còn bảo vệ CSRF
cho request thay đổi trạng thái do token CSRF gắn với session đảm nhận.
"""

from typing import Literal

from fastapi import Response

from app.core.config import Settings
from app.modules.auth.service import IssuedSession

COOKIE_PATH = "/"
COOKIE_SAMESITE: Literal["lax"] = "lax"


def _set_cookie(
    response: Response,
    settings: Settings,
    name: str,
    value: str,
    *,
    httponly: bool,
    max_age: int | None = None,
) -> None:
    response.set_cookie(
        name,
        value,
        max_age=settings.session_ttl_seconds if max_age is None else max_age,
        path=COOKIE_PATH,
        secure=settings.cookie_secure,
        httponly=httponly,
        samesite=COOKIE_SAMESITE,
    )


def set_session_cookies(response: Response, settings: Settings, issued: IssuedSession) -> None:
    # HttpOnly: JavaScript (kể cả script XSS) không đọc được token phiên
    _set_cookie(response, settings, settings.session_cookie_key, issued.token, httponly=True)
    # Không HttpOnly có chủ đích: JS của chính site đọc giá trị này để gửi header X-CSRF-Token
    _set_cookie(response, settings, settings.csrf_cookie_name, issued.csrf_token, httponly=False)


def set_device_cookie(response: Response, settings: Settings, token: str) -> None:
    # HttpOnly: chỉ server cần đọc; không bị xóa khi đăng xuất (đó là mục đích của nó)
    _set_cookie(
        response,
        settings,
        settings.device_cookie_key,
        token,
        httponly=True,
        max_age=settings.trusted_device_ttl_seconds,
    )


def clear_session_cookies(response: Response, settings: Settings) -> None:
    for name, httponly in (
        (settings.session_cookie_key, True),
        (settings.csrf_cookie_name, False),
    ):
        response.delete_cookie(
            name,
            path=COOKIE_PATH,
            secure=settings.cookie_secure,
            httponly=httponly,
            samesite=COOKIE_SAMESITE,
        )
