"""Ánh xạ model_key → provider. Kiểm tra lúc worker khởi động: model đang bật phải có provider."""

from collections.abc import Mapping
from types import MappingProxyType

from app.core.config import Settings
from app.core.storage import LocalStorage
from app.modules.videos.catalog import MOCK_VIDEO_MODEL, VideoModelCatalog
from app.modules.videos.providers.base import VideoProvider
from app.modules.videos.providers.mock import MockProvider


def build_provider_registry(
    catalog: VideoModelCatalog, settings: Settings, storage: LocalStorage
) -> Mapping[str, VideoProvider]:
    available: dict[str, VideoProvider] = {
        MOCK_VIDEO_MODEL.key: MockProvider(
            storage,
            settings.mock_sample_video_file,
            processing_seconds=settings.mock_processing_seconds,
            force_failure=settings.mock_force_failure,
            max_output_bytes=settings.video_output_max_bytes,
        )
    }
    missing = [spec.key for spec in catalog.enabled() if spec.key not in available]
    if missing:
        raise ValueError(f"Model video đang bật nhưng chưa có provider: {', '.join(missing)}")
    return MappingProxyType(available)
