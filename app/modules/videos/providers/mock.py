"""MockProvider (P2-7, W7): bản demo, KHÔNG phải AI thật.

Giả lập tiến độ rồi sao chép file MP4 mẫu do chủ dự án cung cấp (`MOCK_SAMPLE_VIDEO_PATH`) vào
`tmp/` của storage. Không đọc prompt, không gọi mạng. Thiếu file mẫu / sai định dạng / quá lớn →
`ProviderError` không thử lại, với mã lỗi an toàn.
"""

import asyncio
import stat
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

from app.core.storage import LocalStorage, TempFile
from app.modules.videos.providers.base import (
    MOCK_FORCED_FAILURE,
    MOCK_SAMPLE_INVALID,
    MOCK_SAMPLE_MISSING,
    OUTPUT_TOO_LARGE,
    ProgressReporter,
    ProviderError,
    VideoJob,
)

MP4_SIGNATURE = b"ftyp"  # hộp `ftyp` nằm ở byte 4..8 của mọi file MP4
MP4_HEADER_BYTES = 8
READ_CHUNK_BYTES = 1024 * 1024
PROGRESS_STEPS = 4
PROGRESS_BEFORE_OUTPUT = 90  # 100% chỉ được ghi khi kết quả đã lưu xong (CAS hoàn tất)

Sleep = Callable[[float], Awaitable[None]]


def is_mp4_header(data: bytes) -> bool:
    return len(data) >= MP4_HEADER_BYTES and data[4:8] == MP4_SIGNATURE


class MockProvider:
    def __init__(
        self,
        storage: LocalStorage,
        sample_path: Path | None,
        *,
        processing_seconds: float,
        force_failure: bool,
        max_output_bytes: int,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.storage = storage
        self.sample_path = sample_path
        self.processing_seconds = processing_seconds
        self.force_failure = force_failure
        self.max_output_bytes = max_output_bytes
        self._sleep = sleep

    async def generate(self, job: VideoJob, report_progress: ProgressReporter) -> TempFile:
        if self.force_failure:
            raise ProviderError(MOCK_FORCED_FAILURE)
        sample = await asyncio.to_thread(self._checked_sample)
        await self._simulate_progress(report_progress)
        return await self.storage.write_temp_file(self._chunks(sample))

    def _checked_sample(self) -> Path:
        if self.sample_path is None:
            raise ProviderError(MOCK_SAMPLE_MISSING)
        try:
            info = self.sample_path.stat()
        except FileNotFoundError as exc:
            raise ProviderError(MOCK_SAMPLE_MISSING) from exc
        if not stat.S_ISREG(info.st_mode):
            raise ProviderError(MOCK_SAMPLE_MISSING)
        if info.st_size > self.max_output_bytes:
            raise ProviderError(OUTPUT_TOO_LARGE)
        with self.sample_path.open("rb") as handle:
            if not is_mp4_header(handle.read(MP4_HEADER_BYTES)):
                raise ProviderError(MOCK_SAMPLE_INVALID)
        return self.sample_path

    async def _simulate_progress(self, report_progress: ProgressReporter) -> None:
        step_seconds = self.processing_seconds / PROGRESS_STEPS
        for step in range(1, PROGRESS_STEPS + 1):
            await self._sleep(step_seconds)
            await report_progress(step * PROGRESS_BEFORE_OUTPUT // PROGRESS_STEPS)

    async def _chunks(self, sample: Path) -> AsyncIterator[bytes]:
        handle = await asyncio.to_thread(sample.open, "rb")
        total = 0
        try:
            while chunk := await asyncio.to_thread(handle.read, READ_CHUNK_BYTES):
                total += len(chunk)
                if total > self.max_output_bytes:  # file mẫu lớn lên sau khi đã kiểm tra
                    raise ProviderError(OUTPUT_TOO_LARGE)
                yield chunk
        finally:
            # Đóng đồng bộ: không `await` trong finally của async generator (có thể chạy lúc GC)
            handle.close()
