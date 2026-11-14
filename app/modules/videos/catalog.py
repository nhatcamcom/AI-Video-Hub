"""Danh mục model video (P2-5).

- `VideoModelSpec` là nguồn DUY NHẤT về khả năng của một model (tỷ lệ khung hình, thời lượng, có
  nhận ảnh đầu vào hay không). Router, config hay validate của `POST /videos` (P2-6) chỉ đọc từ đây.
- `KNOWN_VIDEO_MODELS` khai báo mọi model mà code biết; cấu hình `ENABLED_VIDEO_MODELS` chỉ chọn
  model nào đang bật.
- `key` là định danh BẤT BIẾN: được lưu cùng mỗi video (P2-6), nên không bao giờ đổi hay dùng lại
  cho model khác; `display_name` chỉ để hiển thị và có thể đổi. Model đã tắt vẫn tra được qua
  `get_known()` để hiển thị video cũ.
- Phase 2 chỉ có `mock`: bản demo, KHÔNG phải nhà cung cấp AI thật, ở mọi môi trường.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from app.core.config import VIDEO_MODEL_KEY_PATTERN


class AspectRatio(StrEnum):
    """Tập tỷ lệ khung hình cho phép trên toàn hệ thống (quyết định 7.5)."""

    LANDSCAPE = "16:9"
    PORTRAIT = "9:16"
    SQUARE = "1:1"


ALLOWED_DURATIONS_SECONDS: tuple[int, ...] = (5, 10)  # quyết định 7.5
_KEY_REGEX = re.compile(VIDEO_MODEL_KEY_PATTERN)


class UnknownVideoModelError(ValueError):
    """ENABLED_VIDEO_MODELS chứa key không có trong danh mục (dừng app khi khởi động)."""


def _require_unique_subset(
    values: tuple[object, ...], allowed: Sequence[object], label: str
) -> None:
    if not values:
        raise ValueError(f"model video phải hỗ trợ ít nhất một {label}")
    if len(set(values)) != len(values):
        raise ValueError(f"model video khai báo trùng {label}")
    if not set(values) <= set(allowed):
        raise ValueError(f"model video khai báo {label} ngoài tập cho phép")


@dataclass(frozen=True)
class VideoModelSpec:
    key: str
    display_name: str
    is_demo: bool
    aspect_ratios: tuple[AspectRatio, ...]
    durations_seconds: tuple[int, ...]
    supports_input_image: bool

    def __post_init__(self) -> None:
        if not _KEY_REGEX.fullmatch(self.key):
            raise ValueError(f"key model video sai định dạng: {self.key!r}")
        if not self.display_name.strip():
            raise ValueError("model video phải có display_name")
        _require_unique_subset(self.aspect_ratios, tuple(AspectRatio), "tỷ lệ khung hình")
        _require_unique_subset(self.durations_seconds, ALLOWED_DURATIONS_SECONDS, "thời lượng")

    def supports(self, aspect_ratio: AspectRatio | str, duration_seconds: int) -> bool:
        """Model có tạo được video với tổ hợp này không (so khớp chính xác, không chuẩn hóa)."""
        if isinstance(duration_seconds, bool) or not isinstance(duration_seconds, int):
            return False
        return aspect_ratio in self.aspect_ratios and duration_seconds in self.durations_seconds


MOCK_VIDEO_MODEL = VideoModelSpec(
    key="mock",
    display_name="Mock (bản demo, không phải AI thật)",
    is_demo=True,
    aspect_ratios=(AspectRatio.LANDSCAPE, AspectRatio.PORTRAIT, AspectRatio.SQUARE),
    durations_seconds=ALLOWED_DURATIONS_SECONDS,
    supports_input_image=True,
)
KNOWN_VIDEO_MODELS: tuple[VideoModelSpec, ...] = (MOCK_VIDEO_MODEL,)


class VideoModelCatalog:
    """Danh mục bất biến: model đã biết và (một phần trong số đó) model đang bật."""

    def __init__(
        self, known: Mapping[str, VideoModelSpec], enabled: tuple[VideoModelSpec, ...]
    ) -> None:
        self._known = MappingProxyType(dict(known))
        self._enabled = enabled
        self._enabled_by_key = MappingProxyType({spec.key: spec for spec in enabled})

    def enabled(self) -> tuple[VideoModelSpec, ...]:
        """Model đang bật, đúng thứ tự cấu hình."""
        return self._enabled

    def get_enabled(self, key: str) -> VideoModelSpec | None:
        return self._enabled_by_key.get(key)

    def get_known(self, key: str) -> VideoModelSpec | None:
        """Kể cả model đã tắt (để hiển thị video đã tạo trước đó)."""
        return self._known.get(key)


def _index_known(known: Sequence[VideoModelSpec]) -> dict[str, VideoModelSpec]:
    by_key = {spec.key: spec for spec in known}
    if len(by_key) != len(known):
        raise ValueError("danh mục model video có key trùng")
    return by_key


def build_video_model_catalog(
    enabled_keys: Sequence[str], known: Sequence[VideoModelSpec] = KNOWN_VIDEO_MODELS
) -> VideoModelCatalog:
    """Dựng danh mục từ cấu hình. Gọi khi khởi động: cấu hình sai → app không chạy.

    Raises:
        ValueError: danh sách rỗng hoặc có key trùng.
        UnknownVideoModelError: có key không nằm trong `known`.
    """
    if not enabled_keys:
        raise ValueError("ENABLED_VIDEO_MODELS không được rỗng")
    if len(set(enabled_keys)) != len(enabled_keys):
        raise ValueError("ENABLED_VIDEO_MODELS có key trùng")
    known_by_key = _index_known(known)
    unknown = [key for key in enabled_keys if key not in known_by_key]
    if unknown:
        raise UnknownVideoModelError(
            f"ENABLED_VIDEO_MODELS có model không tồn tại: {', '.join(unknown)}"
        )
    return VideoModelCatalog(known_by_key, tuple(known_by_key[key] for key in enabled_keys))
