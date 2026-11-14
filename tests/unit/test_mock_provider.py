"""MockProvider (P2-7, W7): bản demo, sao chép MP4 mẫu, KHÔNG gọi AI thật."""

import asyncio
from pathlib import Path

import pytest

from app.core.storage import LocalStorage
from app.modules.videos.catalog import (
    MOCK_VIDEO_MODEL,
    AspectRatio,
    VideoModelSpec,
    build_video_model_catalog,
)
from app.modules.videos.providers.base import (
    MOCK_FORCED_FAILURE,
    MOCK_SAMPLE_INVALID,
    MOCK_SAMPLE_MISSING,
    OUTPUT_TOO_LARGE,
    ProviderError,
    VideoJob,
)
from app.modules.videos.providers.mock import MockProvider, is_mp4_header
from app.modules.videos.providers.registry import build_provider_registry
from tests.conftest import build_settings

pytestmark = pytest.mark.unit

MP4_HEADER = b"\x00\x00\x00\x18ftypmp42"
SAMPLE = MP4_HEADER + b"\x01" * 3000
JOB = VideoJob(
    id=1,
    owner_id=7,
    model_key="mock",
    aspect_ratio="16:9",
    duration_seconds=5,
    input_image_id=None,
    attempt_count=1,
    lease_token="t" * 32,
    prompt="prompt bí mật",
)


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "storage")


def write_sample(tmp_path: Path, data: bytes = SAMPLE) -> Path:
    path = tmp_path / "sample.mp4"
    path.write_bytes(data)
    return path


def provider(storage: LocalStorage, sample: Path | None, **overrides: object) -> MockProvider:
    options: dict[str, object] = {
        "processing_seconds": 0,
        "force_failure": False,
        "max_output_bytes": 1_000_000,
        **overrides,
    }
    return MockProvider(storage, sample, **options)  # type: ignore[arg-type]


async def no_progress(_: int) -> None:
    return None


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (MP4_HEADER, True),
        (b"\x00\x00\x00\x20ftypisom" + b"x" * 4, True),
        (b"\x00\x00\x00\x18ftyp", True),  # đúng 8 byte
        (b"\x00\x00\x00\x18fty", False),  # < 8 byte
        (b"", False),
        (b"ftyp\x00\x00\x00\x18", False),  # sai vị trí
        (b"<html><body>", False),
    ],
)
def test_mp4_header_check_uses_bytes_4_to_8(data: bytes, expected: bool) -> None:
    assert is_mp4_header(data) is expected


async def test_generate_copies_sample_into_temp_file(storage: LocalStorage, tmp_path: Path) -> None:
    temp = await provider(storage, write_sample(tmp_path)).generate(JOB, no_progress)

    assert temp.path.read_bytes() == SAMPLE
    assert temp.size_bytes == len(SAMPLE)
    assert temp.path.parent == storage.temp_dir


async def test_progress_increases_and_stays_below_completion(
    storage: LocalStorage, tmp_path: Path
) -> None:
    reported: list[int] = []

    async def record(value: int) -> None:
        reported.append(value)

    await provider(storage, write_sample(tmp_path)).generate(JOB, record)

    assert reported == sorted(reported) and len(reported) >= 2
    assert reported[0] > 0 and reported[-1] < 100  # 100 chỉ được ghi khi lưu xong kết quả


async def test_processing_time_is_simulated_with_injected_sleep(
    storage: LocalStorage, tmp_path: Path
) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    mock = MockProvider(
        storage,
        write_sample(tmp_path),
        processing_seconds=8,
        force_failure=False,
        max_output_bytes=1_000_000,
        sleep=fake_sleep,
    )
    await mock.generate(JOB, no_progress)

    assert sum(slept) == pytest.approx(8)


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("unset", MOCK_SAMPLE_MISSING),
        ("missing", MOCK_SAMPLE_MISSING),
        ("directory", MOCK_SAMPLE_MISSING),
    ],
)
async def test_missing_sample_fails_without_retry(
    storage: LocalStorage, tmp_path: Path, case: str, code: str
) -> None:
    sample = {"unset": None, "missing": tmp_path / "nope.mp4", "directory": tmp_path}[case]

    with pytest.raises(ProviderError) as caught:
        await provider(storage, sample).generate(JOB, no_progress)

    assert (caught.value.code, caught.value.retryable) == (code, False)
    assert not storage.temp_dir.exists() or list(storage.temp_dir.iterdir()) == []


@pytest.mark.parametrize("data", [b"<html>not a video</html>", b"\x00\x00", b""])
async def test_non_mp4_sample_is_invalid(
    storage: LocalStorage, tmp_path: Path, data: bytes
) -> None:
    with pytest.raises(ProviderError) as caught:
        await provider(storage, write_sample(tmp_path, data)).generate(JOB, no_progress)

    assert (caught.value.code, caught.value.retryable) == (MOCK_SAMPLE_INVALID, False)


async def test_sample_larger_than_limit_is_rejected(storage: LocalStorage, tmp_path: Path) -> None:
    with pytest.raises(ProviderError) as caught:
        await provider(storage, write_sample(tmp_path), max_output_bytes=100).generate(
            JOB, no_progress
        )

    assert (caught.value.code, caught.value.retryable) == (OUTPUT_TOO_LARGE, False)


async def test_sample_growing_during_copy_is_cut_off(
    storage: LocalStorage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sample = write_sample(tmp_path)
    mock = provider(storage, sample, max_output_bytes=len(SAMPLE))

    async def grow(_: int) -> None:
        with sample.open("ab") as handle:  # file lớn lên sau khi đã kiểm tra kích thước
            handle.write(b"\x02" * 10)

    with pytest.raises(ProviderError) as caught:
        await mock.generate(JOB, grow)

    assert caught.value.code == OUTPUT_TOO_LARGE
    assert list(storage.temp_dir.iterdir()) == []  # file tạm dở đã bị xóa


async def test_forced_failure(storage: LocalStorage, tmp_path: Path) -> None:
    with pytest.raises(ProviderError) as caught:
        await provider(storage, write_sample(tmp_path), force_failure=True).generate(
            JOB, no_progress
        )

    assert (caught.value.code, caught.value.retryable) == (MOCK_FORCED_FAILURE, False)


async def test_cancelled_generation_removes_partial_temp_file(
    storage: LocalStorage, tmp_path: Path
) -> None:
    started = asyncio.Event()

    async def block(_: int) -> None:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(provider(storage, write_sample(tmp_path)).generate(JOB, block))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not storage.temp_dir.exists() or list(storage.temp_dir.iterdir()) == []


def test_job_repr_never_shows_prompt_or_lease_token() -> None:
    assert "prompt bí mật" not in repr(JOB)
    assert "t" * 32 not in repr(JOB)


def test_registry_maps_every_enabled_model(storage: LocalStorage) -> None:
    registry = build_provider_registry(
        build_video_model_catalog(["mock"]), build_settings(), storage
    )

    assert set(registry) == {"mock"}
    assert isinstance(registry["mock"], MockProvider)


def test_registry_refuses_enabled_model_without_provider(storage: LocalStorage) -> None:
    orphan = VideoModelSpec(
        key="no-provider",
        display_name="Không có provider",
        is_demo=True,
        aspect_ratios=(AspectRatio.SQUARE,),
        durations_seconds=(5,),
        supports_input_image=False,
    )
    catalog = build_video_model_catalog(["no-provider"], known=(MOCK_VIDEO_MODEL, orphan))

    with pytest.raises(ValueError, match="no-provider"):
        build_provider_registry(catalog, build_settings(), storage)
