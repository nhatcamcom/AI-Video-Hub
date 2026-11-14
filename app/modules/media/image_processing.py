"""Kiểm tra và mã hóa lại ảnh upload bằng Pillow (P2-4).

Thứ tự từ rẻ tới đắt:
1. `detect_image_format`: magic bytes (không tin content_type / tên file của client).
2. `inspect_image`: chỉ đọc header → định dạng Pillow phải khớp magic bytes, từ chối ảnh động và ảnh
   vượt giới hạn pixel TRƯỚC khi giải mã và trước khi xin slot xử lý.
3. `process_image` (chạy trong `ImageProcessingGate`): giải mã đầy đủ, áp hướng xoay EXIF, rồi mã
   hóa lại KHÔNG kèm EXIF/GPS/XMP/text (chỉ giữ ICC). Mã hóa lại cũng vô hiệu hóa file
   "đa định dạng".

Giới hạn pixel dựa trên đo RAM thật (kế hoạch 6.4): JPEG/PNG ~8,2 byte/pixel khi phải xoay, WebP
~34,4 byte/pixel → JPEG/PNG ≤ 24 MP (~190 MiB), WebP ≤ 8 MP (~260 MiB); 2 slot → tối đa ~525 MiB.
"""

import asyncio
import functools
import io
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any

from PIL import Image, ImageOps

from app.core.errors import PayloadTooLargeError
from app.modules.media.errors import (
    AnimatedImageError,
    ImageDimensionsTooLargeError,
    ImageProcessingBusyError,
    InvalidImageError,
)

SNIFF_SIZE_BYTES = 12  # đủ cho chữ ký PNG (8), JPEG (3), WebP (RIFF....WEBP = 12)
PROCESSING_SLOTS = 2  # tối đa 2 ảnh được giải mã / mã hóa thật sự cùng lúc
MAX_WAITING_REQUESTS = 4  # tối đa 4 request chờ slot; request thứ 5 nhận 503 ngay
WAIT_TIMEOUT_SECONDS = 30.0  # chờ slot quá lâu → 503
JPEG_WEBP_QUALITY = 90


class ImageFormat(StrEnum):
    """Giá trị trùng tên định dạng của Pillow."""

    PNG = "PNG"
    JPEG = "JPEG"
    WEBP = "WEBP"


@dataclass(frozen=True)
class FormatSpec:
    content_type: str
    extension: str
    save_options: Mapping[str, Any]


FORMAT_SPECS: Mapping[ImageFormat, FormatSpec] = MappingProxyType(
    {
        ImageFormat.PNG: FormatSpec("image/png", ".png", MappingProxyType({})),
        ImageFormat.JPEG: FormatSpec(
            "image/jpeg", ".jpg", MappingProxyType({"quality": JPEG_WEBP_QUALITY})
        ),
        ImageFormat.WEBP: FormatSpec(
            "image/webp", ".webp", MappingProxyType({"quality": JPEG_WEBP_QUALITY})
        ),
    }
)
MAX_PIXELS: Mapping[ImageFormat, int] = MappingProxyType(
    {ImageFormat.JPEG: 24_000_000, ImageFormat.PNG: 24_000_000, ImageFormat.WEBP: 8_000_000}
)
# Lớp bảo vệ bổ sung của chính Pillow: quá 2 lần mức này → DecompressionBombError ngay khi open()
Image.MAX_IMAGE_PIXELS = max(MAX_PIXELS.values())

# Pillow có thể ném nhiều loại lỗi khi file hỏng / giả mạo / bị cắt cụt
_DECODE_ERRORS = (OSError, SyntaxError, ValueError, EOFError)


@dataclass(frozen=True)
class ProcessedImage:
    data: bytes
    width: int
    height: int


def detect_image_format(head: bytes) -> ImageFormat | None:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ImageFormat.PNG
    if head.startswith(b"\xff\xd8\xff"):
        return ImageFormat.JPEG
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ImageFormat.WEBP
    return None


def _require_within_pixel_limit(
    size: tuple[int, int], image_format: ImageFormat, max_pixels: Mapping[ImageFormat, int]
) -> None:
    width, height = size
    if width <= 0 or height <= 0:
        raise InvalidImageError
    if width * height > max_pixels[image_format]:
        raise ImageDimensionsTooLargeError


def inspect_image(
    path: Path, image_format: ImageFormat, max_pixels: Mapping[ImageFormat, int] = MAX_PIXELS
) -> tuple[int, int]:
    """Đọc kích thước từ header (không giải mã). Chạy được trước khi xin slot xử lý."""
    try:
        # formats=[...]: chỉ dùng đúng bộ giải mã của định dạng đã xác định bằng magic bytes
        with Image.open(path, formats=[image_format.value]) as image:
            _require_within_pixel_limit(image.size, image_format, max_pixels)
            if getattr(image, "is_animated", False):
                raise AnimatedImageError
            return image.size
    except Image.DecompressionBombError as exc:
        raise ImageDimensionsTooLargeError from exc
    except _DECODE_ERRORS as exc:
        raise InvalidImageError from exc


def process_image(path: Path, image_format: ImageFormat, max_output_bytes: int) -> ProcessedImage:
    """Giải mã đầy đủ, áp hướng xoay, mã hóa lại chỉ kèm ICC. Chạy trong thread của gate."""
    spec = FORMAT_SPECS[image_format]
    try:
        with Image.open(path, formats=[image_format.value]) as image:
            _require_within_pixel_limit(image.size, image_format, MAX_PIXELS)
            if getattr(image, "is_animated", False):
                raise AnimatedImageError
            image.load()
            save_options = dict(spec.save_options)
            icc_profile = image.info.get("icc_profile")
            if icc_profile:
                save_options["icc_profile"] = icc_profile
            # in_place: không tạo bản sao khi không cần xoay (giảm RAM đỉnh, xem kế hoạch 6.4)
            ImageOps.exif_transpose(image, in_place=True)
            image.info = {}  # bỏ MỌI metadata đọc từ file gốc; ICC được truyền lại tường minh
            output = io.BytesIO()
            image.save(output, image_format.value, **save_options)
            width, height = image.size
    except Image.DecompressionBombError as exc:
        raise ImageDimensionsTooLargeError from exc
    except _DECODE_ERRORS as exc:
        raise InvalidImageError from exc
    data = output.getvalue()
    if len(data) > max_output_bytes:
        raise PayloadTooLargeError
    return ProcessedImage(data=data, width=width, height=height)


class ImageProcessingGate:
    """Tối đa `slots` job chạy thật sự cùng lúc, tối đa `max_waiting` request chờ (không hàng đợi vô
    hạn). Request đang chờ chỉ giữ đường dẫn file tạm, chưa giải mã gì.

    Slot chỉ được trả khi thread làm xong, kể cả khi request chờ kết quả đã bị hủy — nếu không,
    thread cũ vẫn giải mã trong khi job mới bắt đầu, vượt giới hạn RAM.
    """

    def __init__(
        self,
        slots: int = PROCESSING_SLOTS,
        max_waiting: int = MAX_WAITING_REQUESTS,
        wait_timeout_seconds: float = WAIT_TIMEOUT_SECONDS,
    ) -> None:
        self.slots = slots
        self.max_waiting = max_waiting
        self.wait_timeout_seconds = wait_timeout_seconds
        self._semaphore = asyncio.Semaphore(slots)
        self._executor = ThreadPoolExecutor(
            max_workers=slots, thread_name_prefix="image-processing"
        )
        self._waiting = 0

    @property
    def waiting(self) -> int:
        return self._waiting

    async def run[T](self, function: Callable[..., T], *args: Any) -> T:
        await self._acquire_slot()
        try:
            future = asyncio.get_running_loop().run_in_executor(
                self._executor, functools.partial(function, *args)
            )
        except BaseException:
            self._semaphore.release()
            raise
        future.add_done_callback(lambda _: self._semaphore.release())
        return await asyncio.shield(future)

    async def _acquire_slot(self) -> None:
        if not self._semaphore.locked():
            await self._semaphore.acquire()  # còn slot: lấy ngay, không chờ
            return
        if self._waiting >= self.max_waiting:
            raise ImageProcessingBusyError
        self._waiting += 1
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=self.wait_timeout_seconds)
        except TimeoutError as exc:
            raise ImageProcessingBusyError from exc
        finally:
            self._waiting -= 1

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
