"""Hợp đồng chung giữa worker và provider tạo video (P2-7)."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from app.core.storage import TempFile

# Mã lỗi an toàn để hiển thị (lưu vào videos.error_code)
MOCK_SAMPLE_MISSING = "mock_sample_missing"
MOCK_SAMPLE_INVALID = "mock_sample_invalid"
MOCK_FORCED_FAILURE = "mock_forced_failure"
OUTPUT_TOO_LARGE = "output_too_large"


@dataclass(frozen=True)
class VideoJob:
    """Job đã được claim. Prompt và lease token không bao giờ xuất hiện trong repr / log."""

    id: int
    owner_id: int
    model_key: str
    aspect_ratio: str
    duration_seconds: int
    input_image_id: int | None
    attempt_count: int
    lease_token: str = field(repr=False)
    prompt: str = field(repr=False)


ProgressReporter = Callable[[int], Awaitable[None]]


class ProviderError(Exception):
    """Lỗi có mã an toàn; `retryable=False` → job `failed` ngay, không thử lại."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class VideoProvider(Protocol):
    async def generate(self, job: VideoJob, report_progress: ProgressReporter) -> TempFile:
        """Tạo video vào một file tạm của `LocalStorage` (chưa tính hạn mức, chưa có khóa đích)."""
        ...
