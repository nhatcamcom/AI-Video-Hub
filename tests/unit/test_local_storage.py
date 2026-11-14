"""LocalStorage (P2-3): khóa do server sinh, chống traversal/symlink, ghi nguyên tử, xóa rõ ràng."""

import asyncio
import os
import re
import sys
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from app.core.storage import (
    ALLOWED_EXTENSIONS,
    TEMP_DIR_NAME,
    TEMP_FILE_MAX_AGE_SECONDS,
    DeleteOutcome,
    InvalidStorageKeyError,
    LocalStorage,
    StorageKind,
    TempFile,
    create_storage,
)
from tests.conftest import build_settings

pytestmark = pytest.mark.unit

KEY_FORMAT = re.compile(r"^(uploads|videos|posters)/[0-9a-f]{32}\.(png|jpg|webp|mp4)$")
HEX32 = "0123456789abcdef0123456789abcdef"
OLD_AGE_SECONDS = TEMP_FILE_MAX_AGE_SECONDS + 60


async def chunks_of(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


def make_directory_link(link: Path, target: Path) -> None:
    """Symlink thư mục; Windows thiếu quyền tạo symlink thì dùng junction; không được thì skip."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except OSError:
        pass
    if sys.platform == "win32":
        import _winapi  # chỉ có trên Windows

        _winapi.CreateJunction(str(target), str(link))
        return
    pytest.skip("không tạo được liên kết thư mục trên máy này")


def make_file_symlink(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link)
    except OSError:
        pytest.skip("máy này không cho tạo symlink file (cần Developer Mode / admin)")


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "media")


def set_age(path: Path, seconds: float) -> None:
    moment = time.time() - seconds
    os.utime(path, (moment, moment))


# -------------------- khởi tạo
def test_creating_storage_does_not_touch_the_disk(tmp_path: Path) -> None:
    LocalStorage(tmp_path / "media")

    assert not (tmp_path / "media").exists()


def test_create_storage_uses_resolved_root_from_settings(tmp_path: Path) -> None:
    settings = build_settings(storage_root=str(tmp_path / "configured"))

    assert create_storage(settings).root == (tmp_path / "configured").resolve()


# -------------------- khóa
@pytest.mark.parametrize(
    ("kind", "extension"),
    [
        (kind, extension)
        for kind, extensions in ALLOWED_EXTENSIONS.items()
        for extension in extensions
    ],
)
def test_new_key_is_random_and_server_generated(
    storage: LocalStorage, kind: StorageKind, extension: str
) -> None:
    first = storage.new_key(kind, extension)
    second = storage.new_key(kind, extension)

    assert KEY_FORMAT.fullmatch(first)
    assert first.startswith(f"{kind.value}/") and first.endswith(extension)
    assert first != second


def test_allowed_kinds_and_extensions_are_exactly_the_approved_whitelist() -> None:
    assert {kind.value for kind in StorageKind} == {"uploads", "videos", "posters"}
    assert ALLOWED_EXTENSIONS[StorageKind.UPLOADS] == frozenset({".png", ".jpg", ".webp"})
    assert ALLOWED_EXTENSIONS[StorageKind.VIDEOS] == frozenset({".mp4"})


@pytest.mark.parametrize(
    ("kind", "extension"),
    [
        (StorageKind.UPLOADS, ".exe"),
        (StorageKind.UPLOADS, ".mp4"),
        (StorageKind.VIDEOS, ".png"),
        (StorageKind.UPLOADS, "png"),
        (StorageKind.UPLOADS, ".PNG"),
        (StorageKind.UPLOADS, ".png/../x"),
        ("tmp", ".png"),
        ("dev-outbox", ".png"),
    ],
    ids=[
        "exe",
        "video-ext-in-uploads",
        "image-ext-in-videos",
        "no-dot",
        "upper",
        "traversal",
        "tmp",
        "outbox",
    ],
)
def test_new_key_rejects_kind_or_extension_outside_whitelist(
    storage: LocalStorage, kind: Any, extension: str
) -> None:
    with pytest.raises(InvalidStorageKeyError):
        storage.new_key(kind, extension)


# -------------------- path_for
def test_path_for_valid_key_stays_inside_root(storage: LocalStorage) -> None:
    key = storage.new_key(StorageKind.UPLOADS, ".png")

    path = storage.path_for(key)

    assert path.parent == (storage.root / "uploads")
    assert path.resolve().is_relative_to(storage.root)


@pytest.mark.parametrize(
    "key",
    [
        "../secret.png",
        f"uploads/../tmp/{HEX32}.png",
        f"uploads/../../{HEX32}.png",
        f"/uploads/{HEX32}.png",
        f"C:/uploads/{HEX32}.png",
        f"uploads\\{HEX32}.png",
        f"uploads/{HEX32}.png\\..\\..\\x.png",
        f"uploads/%2e%2e/{HEX32}.png",
        f"uploads/{HEX32}.png\x00.txt",
        f"dev-outbox/{HEX32}.png",
        f"tmp/{HEX32}.png",
        f"UPLOADS/{HEX32}.png",
        f"uploads/{HEX32.upper()}.png",
        f"uploads/{HEX32[:-1]}.png",
        f"uploads/{HEX32}.exe",
        f"videos/{HEX32}.png",
        f"uploads/{HEX32}.mp4",
        f"uploads/sub/{HEX32}.png",
        f"uploads/{HEX32}.png ",
        "",
    ],
    ids=[
        "dot-dot",
        "dot-dot-to-tmp",
        "dot-dot-out",
        "absolute",
        "drive",
        "backslash",
        "trailing-backslash-traversal",
        "encoded-dots",
        "null-byte",
        "dev-outbox",
        "tmp",
        "upper-kind",
        "upper-hex",
        "short-name",
        "bad-ext",
        "image-in-videos",
        "video-in-uploads",
        "nested",
        "trailing-space",
        "empty",
    ],
)
def test_path_for_rejects_keys_outside_strict_format(storage: LocalStorage, key: str) -> None:
    with pytest.raises(InvalidStorageKeyError):
        storage.path_for(key)


@pytest.mark.parametrize("key", [None, 42, b"uploads/x.png"], ids=["none", "int", "bytes"])
def test_path_for_rejects_non_string_keys(storage: LocalStorage, key: Any) -> None:
    with pytest.raises(InvalidStorageKeyError):
        storage.path_for(key)


def test_path_for_rejects_kind_directory_linked_outside_root(
    storage: LocalStorage, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    storage.root.mkdir(parents=True)
    make_directory_link(storage.root / "uploads", outside)

    with pytest.raises(InvalidStorageKeyError):
        storage.path_for(f"uploads/{HEX32}.png")


def test_path_for_rejects_file_symlink_pointing_outside_root(
    storage: LocalStorage, tmp_path: Path
) -> None:
    outside_file = tmp_path / "secret.txt"
    outside_file.write_text("bí mật", encoding="utf-8")
    (storage.root / "uploads").mkdir(parents=True)
    make_file_symlink(storage.root / "uploads" / f"{HEX32}.png", outside_file)

    with pytest.raises(InvalidStorageKeyError):
        storage.path_for(f"uploads/{HEX32}.png")


# -------------------- ghi file tạm
async def test_write_temp_file_counts_real_bytes_inside_tmp(storage: LocalStorage) -> None:
    temp = await storage.write_temp_file(chunks_of(b"abc", b"", b"defgh"))

    assert temp.size_bytes == 8
    assert temp.path.parent == storage.root / TEMP_DIR_NAME
    assert temp.path.read_bytes() == b"abcdefgh"


async def test_write_temp_file_uses_random_server_names(storage: LocalStorage) -> None:
    first = await storage.write_temp_file(chunks_of(b"1"))
    second = await storage.write_temp_file(chunks_of(b"2"))

    assert first.path != second.path
    assert re.fullmatch(r"[0-9a-f]{32}\.part", first.path.name)


async def test_write_error_removes_partial_temp_file(storage: LocalStorage) -> None:
    async def failing_chunks() -> AsyncIterator[bytes]:
        yield b"partial-data"
        raise OSError("đĩa đầy (giả lập)")

    with pytest.raises(OSError, match="giả lập"):
        await storage.write_temp_file(failing_chunks())

    assert list((storage.root / TEMP_DIR_NAME).iterdir()) == []


async def test_cancelled_write_removes_partial_temp_file(storage: LocalStorage) -> None:
    started = asyncio.Event()

    async def slow_chunks() -> AsyncIterator[bytes]:
        yield b"first"
        started.set()
        await asyncio.sleep(3600)
        yield b"never"

    task = asyncio.create_task(storage.write_temp_file(slow_chunks()))
    await started.wait()
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert list((storage.root / TEMP_DIR_NAME).iterdir()) == []


# -------------------- commit / discard
async def test_commit_moves_temp_file_to_final_key(storage: LocalStorage) -> None:
    temp = await storage.write_temp_file(chunks_of(b"video-bytes"))
    key = storage.new_key(StorageKind.VIDEOS, ".mp4")

    final_path = await storage.commit_temp_file(temp, key)

    assert final_path == storage.path_for(key)
    assert final_path.read_bytes() == b"video-bytes"
    assert not temp.path.exists()


async def test_commit_rejects_temp_file_outside_tmp_dir(
    storage: LocalStorage, tmp_path: Path
) -> None:
    stray = tmp_path / "stray.part"
    stray.write_bytes(b"x")

    with pytest.raises(ValueError, match="tmp"):
        await storage.commit_temp_file(
            TempFile(path=stray, size_bytes=1), storage.new_key(StorageKind.UPLOADS, ".png")
        )

    assert stray.exists()


async def test_commit_failure_keeps_temp_file_for_caller_to_discard(
    storage: LocalStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    temp = await storage.write_temp_file(chunks_of(b"data"))

    def failing_replace(*_: object) -> None:
        raise PermissionError("file đang bị khóa (giả lập)")

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(PermissionError):
        await storage.commit_temp_file(temp, storage.new_key(StorageKind.UPLOADS, ".png"))

    assert temp.path.exists()
    await storage.discard_temp_file(temp)
    assert not temp.path.exists()


async def test_discard_missing_temp_file_is_harmless(storage: LocalStorage) -> None:
    temp = await storage.write_temp_file(chunks_of(b"x"))
    await storage.discard_temp_file(temp)

    await storage.discard_temp_file(temp)


# -------------------- xóa
async def test_delete_existing_file_reports_deleted(storage: LocalStorage) -> None:
    key = storage.new_key(StorageKind.UPLOADS, ".png")
    await storage.commit_temp_file(await storage.write_temp_file(chunks_of(b"img")), key)

    outcome = await storage.delete(key)

    assert outcome is DeleteOutcome.DELETED
    assert not storage.path_for(key).exists()


async def test_delete_missing_file_reports_not_found(storage: LocalStorage) -> None:
    outcome = await storage.delete(storage.new_key(StorageKind.UPLOADS, ".png"))

    assert outcome is DeleteOutcome.NOT_FOUND


async def test_delete_failure_reports_failed_and_keeps_file(
    storage: LocalStorage, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    key = storage.new_key(StorageKind.VIDEOS, ".mp4")
    await storage.commit_temp_file(await storage.write_temp_file(chunks_of(b"v")), key)

    def failing_unlink(*_: object) -> None:
        raise PermissionError("file đang được đọc (giả lập)")

    monkeypatch.setattr(os, "unlink", failing_unlink)
    outcome = await storage.delete(key)

    assert outcome is DeleteOutcome.FAILED
    assert storage.path_for(key).exists()
    assert str(storage.root) not in caplog.text  # log không chứa đường dẫn tuyệt đối


async def test_delete_rejects_invalid_key(storage: LocalStorage) -> None:
    with pytest.raises(InvalidStorageKeyError):
        await storage.delete("../../ai_video_hub.db")


# -------------------- dọn file tạm
async def test_remove_stale_temp_files_only_deletes_old_files_in_tmp(storage: LocalStorage) -> None:
    old_temp = await storage.write_temp_file(chunks_of(b"old"))
    fresh_temp = await storage.write_temp_file(chunks_of(b"fresh"))
    set_age(old_temp.path, OLD_AGE_SECONDS)
    old_upload_key = storage.new_key(StorageKind.UPLOADS, ".png")
    old_upload = await storage.commit_temp_file(
        await storage.write_temp_file(chunks_of(b"keep")), old_upload_key
    )
    set_age(old_upload, OLD_AGE_SECONDS)
    nested = storage.root / TEMP_DIR_NAME / "nested"
    nested.mkdir()
    (nested / "inner.part").write_bytes(b"x")
    set_age(nested / "inner.part", OLD_AGE_SECONDS)

    removed = await storage.remove_stale_temp_files()

    assert removed == 1
    assert not old_temp.path.exists()
    assert fresh_temp.path.exists()
    assert old_upload.exists()  # ngoài tmp/: không bao giờ bị dọn
    assert (nested / "inner.part").exists()  # không đi vào thư mục con


async def test_remove_stale_temp_files_does_not_follow_links(
    storage: LocalStorage, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.part"
    victim.write_bytes(b"must survive")
    set_age(victim, OLD_AGE_SECONDS)
    (storage.root / TEMP_DIR_NAME).mkdir(parents=True)
    make_directory_link(storage.root / TEMP_DIR_NAME / "link-out", outside)

    await storage.remove_stale_temp_files()

    assert victim.exists()


async def test_remove_stale_temp_files_skips_file_symlinks(
    storage: LocalStorage, tmp_path: Path
) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"must survive")
    set_age(victim, OLD_AGE_SECONDS)
    (storage.root / TEMP_DIR_NAME).mkdir(parents=True)
    make_file_symlink(storage.root / TEMP_DIR_NAME / "link.part", victim)

    removed = await storage.remove_stale_temp_files()

    assert removed == 0
    assert victim.exists()


async def test_remove_stale_temp_files_without_tmp_dir_returns_zero(storage: LocalStorage) -> None:
    assert await storage.remove_stale_temp_files() == 0
    assert not storage.root.exists()


def test_default_temp_file_max_age_is_one_hour() -> None:
    assert TEMP_FILE_MAX_AGE_SECONDS == 3600


# -------------------- I/O không chặn event loop
async def test_file_io_runs_in_worker_threads(
    storage: LocalStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    offloaded: list[str] = []
    real_to_thread = asyncio.to_thread

    async def recording_to_thread(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        offloaded.append(getattr(function, "__name__", repr(function)))
        return await real_to_thread(function, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", recording_to_thread)
    key = storage.new_key(StorageKind.UPLOADS, ".png")
    temp = await storage.write_temp_file(chunks_of(b"a", b"b"))
    await storage.commit_temp_file(temp, key)
    await storage.delete(key)
    await storage.remove_stale_temp_files()

    assert len(offloaded) >= 6  # mở + ghi từng chunk + đóng, replace, unlink, dọn tmp
