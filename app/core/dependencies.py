"""Dependency dùng chung giữa các module."""

from collections.abc import Sequence
from typing import Annotated

from fastapi import Depends, Request

from app.core.client_ip import RateLimitSubject, resolve_client_ip
from app.core.config import Settings
from app.core.email import EmailSender
from app.core.errors import RateLimitError
from app.core.rate_limit import RateLimitCheck, RateLimiter
from app.core.storage import LocalStorage

FORWARDED_FOR_HEADER = "x-forwarded-for"


def get_app_settings(request: Request) -> Settings:
    """Settings của chính app đang chạy (app có thể được dựng với settings riêng khi test)."""
    settings: Settings = request.app.state.settings
    return settings


SettingsDep = Annotated[Settings, Depends(get_app_settings)]


def get_rate_limiter(request: Request) -> RateLimiter:
    limiter: RateLimiter = request.app.state.rate_limiter
    return limiter


def get_email_sender(request: Request) -> EmailSender:
    sender: EmailSender = request.app.state.email_sender
    return sender


def get_storage(request: Request) -> LocalStorage:
    storage: LocalStorage = request.app.state.storage
    return storage


def get_client_ip(request: Request, settings: SettingsDep) -> str:
    """IP thật của client.

    X-Forwarded-For CHỈ được tin khi kết nối trực tiếp đến từ một proxy trong TRUSTED_PROXY_IPS
    (xem app/core/client_ip.py). Uvicorn phải chạy với --no-proxy-headers để không tự ghi đè
    địa chỉ client trước khi request tới đây.
    """
    peer = request.client.host if request.client else None
    forwarded_for = ",".join(request.headers.getlist(FORWARDED_FOR_HEADER)) or None
    return resolve_client_ip(peer, forwarded_for, settings.trusted_proxy_networks)


ClientIpDep = Annotated[str, Depends(get_client_ip)]


def get_rate_limit_subject(client_ip: ClientIpDep) -> RateLimitSubject:
    """Chủ thể rate limit: IPv4 theo địa chỉ, IPv6 theo /64 kèm mạng /48 chứa nó."""
    return RateLimitSubject.from_client_ip(client_ip)


RateLimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]
EmailSenderDep = Annotated[EmailSender, Depends(get_email_sender)]
StorageDep = Annotated[LocalStorage, Depends(get_storage)]
RateLimitSubjectDep = Annotated[RateLimitSubject, Depends(get_rate_limit_subject)]


def enforce_rate_limits(limiter: RateLimiter, checks: Sequence[RateLimitCheck]) -> None:
    """Kiểm tra mọi rule của một request một lần: bị chặn ở rule nào thì không rule nào bị tính."""
    retry_after_seconds = limiter.hit_all(checks)
    if retry_after_seconds is not None:
        raise RateLimitError(retry_after_seconds=retry_after_seconds)
