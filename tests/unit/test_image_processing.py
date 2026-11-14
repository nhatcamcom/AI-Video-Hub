"""Xử lý ảnh (P2-4): magic bytes, header, giới hạn pixel, mã hóa lại sạch metadata, cổng xử lý."""

import asyncio
import io
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

from app.core.errors import PayloadTooLargeError
from app.modules.media import image_processing
from app.modules.media.errors import (
    AnimatedImageError,
    ImageDimensionsTooLargeError,
    ImageProcessingBusyError,
    InvalidImageError,
    UnsupportedImageTypeError,
)
from app.modules.media.image_processing import (
    FORMAT_SPECS,
    MAX_PIXELS,
    SNIFF_SIZE_BYTES,
    ImageFormat,
    ImageProcessingGate,
    detect_image_format,
    inspect_image,
    process_image,
)
from tests.image_samples import (
    BACKGROUND,
    FORBIDDEN_METADATA,
    ICC_PROFILE,
    TOP_LEFT_COLOR,
    make_animated,
    make_image,
    make_png_header_only,
    metadata_sections,
)

pytestmark = pytest.mark.unit

ALL_FORMATS = [ImageFormat.PNG, ImageFormat.JPEG, ImageFormat.WEBP]
LARGE_OUTPUT_LIMIT = 10 * 1024 * 1024


def write(tmp_path: Path, data: bytes, name: str = "upload.part") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def colors_close(actual: object, expected: tuple[int, ...], tolerance: int = 40) -> bool:
    assert isinstance(actual, tuple)
    return all(abs(a - b) <= tolerance for a, b in zip(actual, expected, strict=False))


# --------------------------------------------------------------------------- cấu hình đã duyệt
def test_pixel_limits_match_approved_decision() -> None:
    assert MAX_PIXELS[ImageFormat.JPEG] == 24_000_000
    assert MAX_PIXELS[ImageFormat.PNG] == 24_000_000
    assert MAX_PIXELS[ImageFormat.WEBP] == 8_000_000


def test_pillow_global_bomb_guard_is_kept_as_extra_layer() -> None:
    assert max(MAX_PIXELS.values()) == Image.MAX_IMAGE_PIXELS


def test_format_specs_are_server_controlled() -> None:
    assert {spec.content_type for spec in FORMAT_SPECS.values()} == {
        "image/png",
        "image/jpeg",
        "image/webp",
    }
    assert {spec.extension for spec in FORMAT_SPECS.values()} == {".png", ".jpg", ".webp"}


def test_gate_defaults_match_approved_decision() -> None:
    gate = ImageProcessingGate()
    try:
        assert (gate.slots, gate.max_waiting, gate.wait_timeout_seconds) == (2, 4, 30.0)
    finally:
        gate.close()


# --------------------------------------------------------------------------- magic bytes
@pytest.mark.parametrize("fmt", ALL_FORMATS)
def test_detect_image_format_by_magic_bytes(fmt: ImageFormat) -> None:
    data = make_image(fmt.value, with_metadata=False)

    assert detect_image_format(data[:SNIFF_SIZE_BYTES]) is fmt


@pytest.mark.parametrize(
    "head",
    [
        b"<!doctype html><script>",
        b"GIF89a\x01\x00\x01\x00",
        b"RIFF\x24\x00\x00\x00WAVEfmt ",
        b"",
        b"\x89PN",
    ],
    ids=["html", "gif", "riff-wav", "empty", "short-png"],
)
def test_detect_image_format_rejects_non_whitelisted(head: bytes) -> None:
    assert detect_image_format(head) is None


# --------------------------------------------------------------------------- đọc header
@pytest.mark.parametrize("fmt", ALL_FORMATS)
def test_inspect_reads_dimensions_from_header(tmp_path: Path, fmt: ImageFormat) -> None:
    path = write(tmp_path, make_image(fmt.value, (64, 48), with_metadata=False))

    assert inspect_image(path, fmt) == (64, 48)


def test_inspect_rejects_content_that_does_not_match_detected_format(tmp_path: Path) -> None:
    path = write(tmp_path, make_image("JPEG", with_metadata=False))

    with pytest.raises((UnsupportedImageTypeError, InvalidImageError)):
        inspect_image(path, ImageFormat.PNG)


def test_inspect_rejects_magic_bytes_followed_by_garbage(tmp_path: Path) -> None:
    path = write(tmp_path, b"\x89PNG\r\n\x1a\n" + b"<html>not an image</html>" * 10)

    with pytest.raises(InvalidImageError):
        inspect_image(path, ImageFormat.PNG)


@pytest.mark.parametrize("fmt", [ImageFormat.WEBP, ImageFormat.PNG])
def test_inspect_rejects_animated_images(tmp_path: Path, fmt: ImageFormat) -> None:
    path = write(tmp_path, make_animated(fmt.value))

    with pytest.raises(AnimatedImageError):
        inspect_image(path, fmt)


@pytest.mark.parametrize("fmt", ALL_FORMATS)
def test_inspect_enforces_per_format_pixel_limit(tmp_path: Path, fmt: ImageFormat) -> None:
    path = write(tmp_path, make_image(fmt.value, (100, 50), with_metadata=False))

    assert inspect_image(path, fmt, max_pixels={fmt: 5_000}) == (100, 50)  # đúng bằng giới hạn
    with pytest.raises(ImageDimensionsTooLargeError):
        inspect_image(path, fmt, max_pixels={fmt: 4_999})


# Pillow chỉ cảnh báo (không chặn) khi ảnh vượt MAX_IMAGE_PIXELS (dưới 2 lần);
# kiểm tra riêng của ta vẫn từ chối
@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
@pytest.mark.parametrize(
    ("width", "height"),
    [(5_000, 5_000), (10_000, 10_000), (60_000, 60_000)],
    ids=["25MP-over-limit", "100MP-bomb", "3600MP-bomb"],
)
def test_inspect_rejects_dimension_bombs_without_decoding(
    tmp_path: Path, width: int, height: int
) -> None:
    path = write(tmp_path, make_png_header_only(width, height))
    started = time.perf_counter()

    with pytest.raises(ImageDimensionsTooLargeError):
        inspect_image(path, ImageFormat.PNG)

    assert time.perf_counter() - started < 1.0  # chỉ đọc header, không giải mã


# --------------------------------------------------------------------------- mã hóa lại
@pytest.mark.parametrize("fmt", ALL_FORMATS)
def test_process_removes_exif_gps_xmp_and_text(tmp_path: Path, fmt: ImageFormat) -> None:
    source = make_image(fmt.value)
    before = set(metadata_sections(source, fmt.value))
    assert before & FORBIDDEN_METADATA[fmt.value]  # ảnh mẫu thật sự có metadata

    result = process_image(write(tmp_path, source), fmt, LARGE_OUTPUT_LIMIT)

    after = metadata_sections(result.data, fmt.value)
    assert set(after).isdisjoint(FORBIDDEN_METADATA[fmt.value]), after
    with Image.open(io.BytesIO(result.data)) as reopened:
        assert dict(reopened.getexif()) == {}
        assert "xmp" not in reopened.info
        text_keys = [key.lower() for key in reopened.info if isinstance(key, str)]
        assert not any(key.startswith(("comment", "xml")) for key in text_keys)


@pytest.mark.parametrize("fmt", ALL_FORMATS)
def test_process_keeps_icc_profile(tmp_path: Path, fmt: ImageFormat) -> None:
    result = process_image(write(tmp_path, make_image(fmt.value)), fmt, LARGE_OUTPUT_LIMIT)

    with Image.open(io.BytesIO(result.data)) as reopened:
        assert reopened.info.get("icc_profile") == ICC_PROFILE


@pytest.mark.parametrize("fmt", ALL_FORMATS)
def test_process_applies_exif_orientation_before_saving(tmp_path: Path, fmt: ImageFormat) -> None:
    # Orientation=6: hiển thị xoay 90° theo chiều kim đồng hồ → 60x40 thành 40x60
    result = process_image(
        write(tmp_path, make_image(fmt.value, (60, 40), orientation=6)), fmt, LARGE_OUTPUT_LIMIT
    )

    assert (result.width, result.height) == (40, 60)
    with Image.open(io.BytesIO(result.data)) as reopened:
        rgb = reopened.convert("RGB")
        assert rgb.size == (40, 60)
        assert colors_close(rgb.getpixel((rgb.width - 3, 2)), TOP_LEFT_COLOR)  # xanh sang góc phải
        assert colors_close(rgb.getpixel((2, 2)), BACKGROUND)


@pytest.mark.parametrize("fmt", ALL_FORMATS)
def test_process_without_orientation_keeps_dimensions(tmp_path: Path, fmt: ImageFormat) -> None:
    result = process_image(
        write(tmp_path, make_image(fmt.value, (60, 40), orientation=1)), fmt, LARGE_OUTPUT_LIMIT
    )

    assert (result.width, result.height) == (60, 40)


@pytest.mark.parametrize("fmt", [ImageFormat.PNG, ImageFormat.WEBP])
def test_process_keeps_transparency(tmp_path: Path, fmt: ImageFormat) -> None:
    image = Image.new("RGBA", (30, 30), (0, 0, 0, 0))
    image.paste((255, 0, 0, 255), (0, 0, 10, 10))
    buffer = io.BytesIO()
    options = {"lossless": True} if fmt is ImageFormat.WEBP else {}
    image.save(buffer, fmt.value, **options)

    result = process_image(write(tmp_path, buffer.getvalue()), fmt, LARGE_OUTPUT_LIMIT)

    with Image.open(io.BytesIO(result.data)) as reopened:
        assert reopened.mode == "RGBA"
        pixel = reopened.getpixel((25, 25))
        assert isinstance(pixel, tuple)
        assert pixel[3] == 0


@pytest.mark.parametrize("fmt", ALL_FORMATS)
def test_process_output_keeps_detected_format(tmp_path: Path, fmt: ImageFormat) -> None:
    result = process_image(write(tmp_path, make_image(fmt.value)), fmt, LARGE_OUTPUT_LIMIT)

    assert detect_image_format(result.data[:SNIFF_SIZE_BYTES]) is fmt


@pytest.mark.parametrize("fmt", [ImageFormat.JPEG, ImageFormat.PNG])
def test_process_rejects_truncated_image(tmp_path: Path, fmt: ImageFormat) -> None:
    complete = make_image(fmt.value, (400, 300), with_metadata=False)

    with pytest.raises(InvalidImageError):
        process_image(write(tmp_path, complete[: len(complete) // 2]), fmt, LARGE_OUTPUT_LIMIT)


def test_process_rejects_output_larger_than_limit(tmp_path: Path) -> None:
    path = write(tmp_path, make_image("PNG", (200, 200), with_metadata=False))

    with pytest.raises(PayloadTooLargeError):
        process_image(path, ImageFormat.PNG, max_output_bytes=10)


def test_process_rejects_bomb_even_if_header_check_was_skipped(tmp_path: Path) -> None:
    with pytest.raises(ImageDimensionsTooLargeError):
        process_image(write(tmp_path, make_png_header_only(10_000, 10_000)), ImageFormat.PNG, 100)


# ----------------------------------------------------------------------- cổng xử lý (2 slot, 4 chờ)
async def test_gate_never_runs_more_than_two_jobs_at_once() -> None:
    gate = ImageProcessingGate(slots=2, max_waiting=10, wait_timeout_seconds=5)
    active, peak = 0, 0
    lock = threading.Lock()

    def job() -> None:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1

    try:
        await asyncio.gather(*(gate.run(job) for _ in range(8)))
    finally:
        gate.close()
    assert peak == 2


async def test_gate_rejects_immediately_when_four_requests_already_wait() -> None:
    gate = ImageProcessingGate(slots=2, max_waiting=4, wait_timeout_seconds=5)
    release = threading.Event()
    tasks = [asyncio.create_task(gate.run(release.wait)) for _ in range(6)]  # 2 chạy + 4 chờ
    await asyncio.sleep(0.1)
    try:
        started = time.perf_counter()
        with pytest.raises(ImageProcessingBusyError) as caught:
            await gate.run(release.wait)
        assert time.perf_counter() - started < 0.5  # không xếp hàng thêm
        assert caught.value.status_code == 503
        assert caught.value.code == "image_processing_busy"
        assert int(caught.value.headers["Retry-After"]) > 0
    finally:
        release.set()
        await asyncio.gather(*tasks)
        gate.close()


async def test_gate_times_out_waiting_requests_with_busy_error() -> None:
    gate = ImageProcessingGate(slots=1, max_waiting=4, wait_timeout_seconds=0.2)
    release = threading.Event()
    holder = asyncio.create_task(gate.run(release.wait))
    await asyncio.sleep(0.05)
    try:
        with pytest.raises(ImageProcessingBusyError):
            await gate.run(release.wait)
        assert gate.waiting == 0  # hết hạn chờ thì không còn tính là đang chờ
    finally:
        release.set()
        await holder
        gate.close()


async def test_gate_keeps_slot_until_worker_thread_really_finishes() -> None:
    gate = ImageProcessingGate(slots=1, max_waiting=4, wait_timeout_seconds=0.1)
    release = threading.Event()
    first = asyncio.create_task(gate.run(release.wait))
    await asyncio.sleep(0.05)
    first.cancel()  # request bị hủy nhưng thread vẫn đang giải mã
    with pytest.raises(asyncio.CancelledError):
        await first
    try:
        with pytest.raises(ImageProcessingBusyError):  # slot vẫn bị giữ → không chạy quá 1 job
            await gate.run(release.wait)
    finally:
        release.set()
        await asyncio.sleep(0.1)
        assert await gate.run(lambda: "xong") == "xong"  # slot được trả khi thread kết thúc
        gate.close()


async def test_gate_propagates_job_errors_and_frees_slot() -> None:
    gate = ImageProcessingGate(slots=1, max_waiting=1, wait_timeout_seconds=1)

    def failing() -> None:
        raise InvalidImageError

    try:
        with pytest.raises(InvalidImageError):
            await gate.run(failing)
        assert await gate.run(lambda: 42) == 42
    finally:
        gate.close()


def test_module_exports_are_stable() -> None:
    assert image_processing.SNIFF_SIZE_BYTES == 12
