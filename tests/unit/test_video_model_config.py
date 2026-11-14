"""Cấu hình ENABLED_VIDEO_MODELS (P2-5): mặc định, đọc JSON từ môi trường, từ chối giá trị sai."""

import pytest
from pydantic import ValidationError

from app.main import create_app
from app.modules.videos.catalog import UnknownVideoModelError
from tests.conftest import build_production_settings, build_settings

pytestmark = pytest.mark.unit


def test_mock_is_enabled_by_default() -> None:
    assert build_settings().enabled_video_models == ["mock"]


def test_enabled_models_are_read_from_environment_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLED_VIDEO_MODELS", '["mock"]')

    assert build_settings().enabled_video_models == ["mock"]


def test_environment_value_keeps_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLED_VIDEO_MODELS", '["model-b", "model-a"]')

    assert build_settings().enabled_video_models == ["model-b", "model-a"]


def test_non_json_environment_value_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLED_VIDEO_MODELS", "mock")

    with pytest.raises(Exception, match="enabled_video_models"):
        build_settings()


def test_empty_list_is_rejected() -> None:
    with pytest.raises(ValidationError, match="rỗng"):
        build_settings(enabled_video_models=[])


def test_empty_list_from_environment_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLED_VIDEO_MODELS", "[]")

    with pytest.raises(ValidationError, match="rỗng"):
        build_settings()


def test_duplicate_keys_are_rejected() -> None:
    with pytest.raises(ValidationError, match="trùng"):
        build_settings(enabled_video_models=["mock", "mock"])


@pytest.mark.parametrize(
    "key",
    ["", "MOCK", "Mock", "../mock", "mock/x", "a b", "-mock", "mock_x", "m" * 33, "mock\x00"],
    ids=[
        "empty",
        "upper",
        "mixed-case",
        "traversal",
        "slash",
        "space",
        "leading-dash",
        "underscore",
        "too-long",
        "null-byte",
    ],
)
def test_malformed_keys_are_rejected(key: str) -> None:
    with pytest.raises(ValidationError, match="định dạng"):
        build_settings(enabled_video_models=[key])


def test_longest_allowed_key_passes_format_check() -> None:
    assert build_settings(enabled_video_models=["m" * 32]).enabled_video_models == ["m" * 32]


def test_unknown_key_stops_app_startup() -> None:
    # Định dạng hợp lệ nhưng không có trong danh mục: config (core) không biết danh mục của miền
    # videos, nên create_app kiểm tra và dừng ngay khi khởi động
    with pytest.raises(UnknownVideoModelError, match="sora"):
        create_app(build_settings(enabled_video_models=["mock", "sora"]))


def test_production_keeps_mock_as_demo() -> None:
    app = create_app(build_production_settings())
    try:
        [mock] = app.state.video_model_catalog.enabled()
    finally:
        app.state.image_gate.close()

    assert mock.key == "mock"
    assert mock.is_demo is True
    assert "không phải AI thật" in mock.display_name
