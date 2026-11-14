"""Lưu file người dùng trên đĩa cục bộ (P2-3).

Bảo đảm an toàn:
- Khóa file (`<kind>/<32 hex><đuôi>`) LUÔN do server sinh bằng `secrets`; không bao giờ ghép tên hay
  đường dẫn từ dữ liệu người dùng. Kind và đuôi file chỉ nằm trong danh sách cho phép.
- `path_for` kiểm tra khóa bằng regex chặt RỒI kiểm tra lại đường dẫn sau khi resolve: chặn `..`,
  đường dẫn tuyệt đối, `\\`, byte null, thư mục ngoài danh sách (tmp/, dev-outbox/…) và cả symlink /
  junction trỏ ra ngoài.
- Ghi vào `tmp/` rồi `os.replace` sang vị trí chính: `tmp/` nằm dưới cùng thư mục gốc (cùng ổ
  đĩa) nên thao tác đổi chỗ là nguyên tử — người đọc không bao giờ thấy file ghi dở.
- Mọi thao tác đĩa chạy qua `asyncio.to_thread`, không chặn event loop.
"""

import asyncio
import logging
import os
import re
import secrets
import time
from collections.abc import AsyncIterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import BinaryIO

from app.core.config import Settings

logger = logging.getLogger(__name__)

TEMP_DIR_NAME = "tmp"
TEMP_FILE_SUFFIX = ".part"
KEY_TOKEN_BYTES = 16  # 128 bit ngẫu nhiên → 32 ký tự hex
TEMP_FILE_MAX_AGE_SECONDS = 60 * 60  # file tạm cũ hơn 1 giờ chắc chắn là của upload đã bỏ dở


class StorageKind(StrEnum):
    """Thư mục con được phép. tmp/ và dev-outbox/ cố ý KHÔNG có ở đây."""

    UPLOADS = "uploads"
    VIDEOS = "videos"
    POSTERS = "posters"


ALLOWED_EXTENSIONS: Mapping[StorageKind, frozenset[str]] = MappingProxyType(
    {
        StorageKind.UPLOADS: frozenset({".png", ".jpg", ".webp"}),
        StorageKind.VIDEOS: frozenset({".mp4"}),
        StorageKind.POSTERS: frozenset({".png", ".jpg", ".webp"}),
    }
)
_KEY_PATTERN = re.compile(r"(uploads|videos|posters)/([0-9a-f]{32})(\.[a-z0-9]+)")


class InvalidStorageKeyError(ValueError):
    """Khóa / kind / đuôi file không hợp lệ hoặc trỏ ra ngoài thư mục lưu trữ."""


class DeleteOutcome(StrEnum):
    DELETED = "deleted"
    NOT_FOUND = "not_found"  # file đã không còn → coi như đã xóa khi giải phóng hạn mức
    FAILED = "failed"  # vẫn còn trên đĩa → KHÔNG giải phóng hạn mức (quyết định 7.7)


@dataclass(frozen=True)
class TempFile:
    path: Path
    size_bytes: int  # số byte thực đã ghi xuống đĩa, không phải kích thước client khai báo


def _open_new_file(path: Path) -> BinaryIO:
    return path.open("xb")  # "x": không bao giờ ghi đè file đã có


def _close_and_remove(handle: BinaryIO, path: Path) -> None:
    handle.close()
    path.unlink(missing_ok=True)


class LocalStorage:
    def __init__(self, root: Path) -> None:
        # Chỉ tính đường dẫn; thư mục được tạo khi ghi file đầu tiên
        self.root = root.resolve()
        self.temp_dir = self.root / TEMP_DIR_NAME

    def new_key(self, kind: StorageKind | str, extension: str) -> str:
        """Khóa mới, ngẫu nhiên, do server sinh. Kind/đuôi ngoài danh sách cho phép bị từ chối."""
        try:
            storage_kind = StorageKind(kind)
        except ValueError as exc:
            raise InvalidStorageKeyError("kind không được phép") from exc
        if extension not in ALLOWED_EXTENSIONS[storage_kind]:
            raise InvalidStorageKeyError("đuôi file không được phép cho kind này")
        return f"{storage_kind.value}/{secrets.token_hex(KEY_TOKEN_BYTES)}{extension}"

    def path_for(self, key: object) -> Path:
        """Đường dẫn tuyệt đối của một khóa hợp lệ, bảo đảm nằm trong thư mục kind tương ứng."""
        match = _KEY_PATTERN.fullmatch(key) if isinstance(key, str) else None
        if match is None:
            raise InvalidStorageKeyError("khóa lưu trữ không đúng định dạng")
        kind = StorageKind(match.group(1))
        extension = match.group(3)
        if extension not in ALLOWED_EXTENSIONS[kind]:
            raise InvalidStorageKeyError("đuôi file không được phép cho kind này")
        kind_dir = self.root / kind.value
        path = kind_dir / f"{match.group(2)}{extension}"
        # Symlink / junction (ở thư mục kind hoặc ở chính file) trỏ ra ngoài → resolve sẽ lộ ra
        if path.resolve().parent != kind_dir:
            raise InvalidStorageKeyError("đường dẫn lưu trữ trỏ ra ngoài thư mục cho phép")
        return path

    def _require_temp_file(self, temp: TempFile) -> None:
        if temp.path.resolve().parent != self.temp_dir:
            raise ValueError("file tạm phải nằm trực tiếp trong thư mục tmp/ của storage")

    async def write_temp_file(self, chunks: AsyncIterable[bytes]) -> TempFile:
        """Ghi luồng dữ liệu vào tmp/, đếm byte thật. Lỗi hoặc bị hủy giữa chừng → xóa file dở."""
        await asyncio.to_thread(self.temp_dir.mkdir, parents=True, exist_ok=True)
        path = self.temp_dir / f"{secrets.token_hex(KEY_TOKEN_BYTES)}{TEMP_FILE_SUFFIX}"
        handle = await asyncio.to_thread(_open_new_file, path)
        size_bytes = 0
        try:
            async for chunk in chunks:
                if chunk:
                    await asyncio.to_thread(handle.write, chunk)
                    size_bytes += len(chunk)
        except BaseException:
            # shield: kể cả khi task bị hủy lần nữa, việc dọn file dở vẫn chạy xong trong thread
            await asyncio.shield(asyncio.to_thread(_close_and_remove, handle, path))
            raise
        await asyncio.to_thread(handle.close)
        return TempFile(path=path, size_bytes=size_bytes)

    async def commit_temp_file(self, temp: TempFile, key: str) -> Path:
        """Chuyển file tạm sang vị trí của khóa (nguyên tử). Lỗi → file tạm vẫn còn để phía gọi
        hoàn lại hạn mức rồi `discard_temp_file`."""
        destination = self.path_for(key)
        self._require_temp_file(temp)
        await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(os.replace, temp.path, destination)
        return destination

    async def discard_temp_file(self, temp: TempFile) -> None:
        self._require_temp_file(temp)
        await asyncio.to_thread(temp.path.unlink, missing_ok=True)

    async def delete(self, key: str) -> DeleteOutcome:
        path = self.path_for(key)
        try:
            await asyncio.to_thread(os.unlink, path)
        except FileNotFoundError:
            return DeleteOutcome.NOT_FOUND
        except OSError as exc:
            # Chỉ ghi khóa tương đối và loại lỗi: message của OSError chứa đường dẫn tuyệt đối
            logger.warning("Không xóa được file lưu trữ %s (%s)", key, type(exc).__name__)
            return DeleteOutcome.FAILED
        return DeleteOutcome.DELETED

    async def remove_stale_temp_files(
        self, max_age_seconds: float = TEMP_FILE_MAX_AGE_SECONDS
    ) -> int:
        """Xóa file tạm cũ hơn `max_age_seconds` CHỈ trong tmp/. Trả số file đã xóa."""
        return await asyncio.to_thread(self._remove_stale_temp_files, max_age_seconds, time.time())

    def _remove_stale_temp_files(self, max_age_seconds: float, now: float) -> int:
        if not self.temp_dir.is_dir() or self.temp_dir.is_symlink() or self.temp_dir.is_junction():
            return 0
        with os.scandir(self.temp_dir) as iterator:
            entries = list(iterator)
        removed = 0
        for entry in entries:
            # Không theo symlink/junction, không vào thư mục con: chỉ file thường ngay trong tmp/
            if (
                entry.is_symlink()
                or entry.is_junction()
                or not entry.is_file(follow_symlinks=False)
            ):
                continue
            if now - entry.stat(follow_symlinks=False).st_mtime < max_age_seconds:
                continue
            try:
                os.unlink(entry.path)
            except FileNotFoundError:
                continue
            except OSError as exc:
                logger.warning("Không xóa được file tạm %s (%s)", entry.name, type(exc).__name__)
                continue
            removed += 1
        return removed


def create_storage(settings: Settings) -> LocalStorage:
    return LocalStorage(settings.storage_root_path)
