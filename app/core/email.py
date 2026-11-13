"""Gửi email qua adapter — đổi nhà cung cấp (SMTP, SES, SendGrid…) mà không sửa nghiệp vụ.

Backend hiện có:
- `file`: ghi mỗi email thành một file .txt trong thư mục outbox (chỉ dev; thư mục bị git ignore).
- `memory`: giữ email trong RAM (test).
- `disabled`: không gửi gì (production cho tới khi tích hợp nhà cung cấp thật).
"""

import asyncio
import logging
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.core.config import Settings
from app.core.time import utc_now

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmailMessage:
    to: str
    subject: str
    body: str


class EmailSender(Protocol):
    async def send(self, message: EmailMessage) -> None: ...


class InMemoryEmailSender:
    def __init__(self) -> None:
        self.messages: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> None:
        self.messages.append(message)


class FileEmailSender:
    def __init__(self, outbox_dir: Path) -> None:
        self._outbox_dir = outbox_dir

    async def send(self, message: EmailMessage) -> None:
        # Ghi file là I/O chặn → chạy trong thread, không chặn event loop
        await asyncio.to_thread(self._write, message)

    def _write(self, message: EmailMessage) -> None:
        self._outbox_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{utc_now():%Y%m%dT%H%M%S%fZ}-{secrets.token_hex(4)}.txt"
        content = f"To: {message.to}\nSubject: {message.subject}\n\n{message.body}\n"
        (self._outbox_dir / filename).write_text(content, encoding="utf-8")


class DisabledEmailSender:
    async def send(self, message: EmailMessage) -> None:
        # Không log người nhận hay nội dung (có thể chứa token)
        logger.warning("Email backend is disabled; message %r was not sent", message.subject)


def create_email_sender(settings: Settings) -> EmailSender:
    if settings.email_backend == "memory":
        return InMemoryEmailSender()
    if settings.email_backend == "file":
        return FileEmailSender(Path(settings.email_outbox_dir))
    return DisabledEmailSender()
