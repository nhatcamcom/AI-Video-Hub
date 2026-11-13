import logging
from pathlib import Path

import pytest

from app.core.email import (
    DisabledEmailSender,
    EmailMessage,
    FileEmailSender,
    InMemoryEmailSender,
    create_email_sender,
)
from tests.conftest import build_production_settings, build_settings

pytestmark = pytest.mark.unit

MESSAGE = EmailMessage(to="bob@example.com", subject="Xin chào", body="Nội dung bí mật 123")


async def test_in_memory_sender_keeps_messages() -> None:
    sender = InMemoryEmailSender()

    await sender.send(MESSAGE)

    assert sender.messages == [MESSAGE]


async def test_file_sender_writes_one_file_per_message(tmp_path: Path) -> None:
    sender = FileEmailSender(tmp_path / "outbox")

    await sender.send(MESSAGE)
    await sender.send(MESSAGE)

    files = sorted((tmp_path / "outbox").glob("*.txt"))
    assert len(files) == 2
    content = files[0].read_text(encoding="utf-8")
    assert "To: bob@example.com" in content
    assert "Subject: Xin chào" in content
    assert "Nội dung bí mật 123" in content


async def test_disabled_sender_drops_message_without_logging_its_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        await DisabledEmailSender().send(MESSAGE)

    assert MESSAGE.body not in caplog.text
    assert MESSAGE.to not in caplog.text


@pytest.mark.parametrize(
    ("backend", "expected_type"),
    [("memory", InMemoryEmailSender), ("file", FileEmailSender)],
)
def test_factory_builds_backend_from_settings(backend: str, expected_type: type) -> None:
    assert isinstance(create_email_sender(build_settings(email_backend=backend)), expected_type)


def test_factory_builds_disabled_sender_for_production() -> None:
    assert isinstance(create_email_sender(build_production_settings()), DisabledEmailSender)
