"""Danh mục model video (P2-5): khả năng model khai báo trong code, cấu hình chỉ chọn model bật."""

import dataclasses

import pytest

from app.modules.videos.catalog import (
    ALLOWED_DURATIONS_SECONDS,
    KNOWN_VIDEO_MODELS,
    MOCK_VIDEO_MODEL,
    AspectRatio,
    UnknownVideoModelError,
    VideoModelSpec,
    build_video_model_catalog,
)

pytestmark = pytest.mark.unit

FAKE_ALPHA = VideoModelSpec(
    key="fake-alpha",
    display_name="Fake Alpha",
    is_demo=True,
    aspect_ratios=(AspectRatio.LANDSCAPE,),
    durations_seconds=(5,),
    supports_input_image=False,
)
FAKE_BETA = VideoModelSpec(
    key="fake-beta",
    display_name="Fake Beta",
    is_demo=True,
    aspect_ratios=(AspectRatio.SQUARE, AspectRatio.PORTRAIT),
    durations_seconds=(10,),
    supports_input_image=True,
)
TEST_KNOWN = (MOCK_VIDEO_MODEL, FAKE_ALPHA, FAKE_BETA)


# --------------------------------------------------------------------------- giá trị đã duyệt
def test_global_capability_sets_match_decision_7_5() -> None:
    assert [ratio.value for ratio in AspectRatio] == ["16:9", "9:16", "1:1"]
    assert ALLOWED_DURATIONS_SECONDS == (5, 10)


def test_mock_model_declares_every_capability() -> None:
    assert MOCK_VIDEO_MODEL.key == "mock"
    assert MOCK_VIDEO_MODEL.is_demo is True
    assert MOCK_VIDEO_MODEL.aspect_ratios == (
        AspectRatio.LANDSCAPE,
        AspectRatio.PORTRAIT,
        AspectRatio.SQUARE,
    )
    assert MOCK_VIDEO_MODEL.durations_seconds == (5, 10)
    assert MOCK_VIDEO_MODEL.supports_input_image is True


def test_mock_display_name_says_it_is_not_real_ai() -> None:
    name = MOCK_VIDEO_MODEL.display_name.lower()

    assert "demo" in name
    assert "không phải ai thật" in name


def test_known_models_contain_only_mock_in_phase_2() -> None:
    assert [spec.key for spec in KNOWN_VIDEO_MODELS] == ["mock"]


@pytest.mark.parametrize("spec", KNOWN_VIDEO_MODELS, ids=lambda spec: spec.key)
def test_known_models_only_use_allowed_capabilities(spec: VideoModelSpec) -> None:
    assert spec.aspect_ratios and set(spec.aspect_ratios) <= set(AspectRatio)
    assert spec.durations_seconds and set(spec.durations_seconds) <= set(ALLOWED_DURATIONS_SECONDS)
    assert isinstance(spec.aspect_ratios, tuple)
    assert isinstance(spec.durations_seconds, tuple)


def test_spec_is_immutable() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        MOCK_VIDEO_MODEL.is_demo = False  # type: ignore[misc]


# --------------------------------------------------------------------------- kiểm tra khi khai báo
@pytest.mark.parametrize(
    "overrides",
    [
        {"key": "MOCK"},
        {"key": ""},
        {"key": "../x"},
        {"aspect_ratios": ()},
        {"aspect_ratios": ("4:3",)},
        {"aspect_ratios": (AspectRatio.SQUARE, AspectRatio.SQUARE)},
        {"durations_seconds": ()},
        {"durations_seconds": (7,)},
        {"durations_seconds": (5, 5)},
        {"display_name": " "},
    ],
    ids=[
        "uppercase-key",
        "empty-key",
        "path-key",
        "no-ratio",
        "ratio-outside-set",
        "duplicate-ratio",
        "no-duration",
        "duration-outside-set",
        "duplicate-duration",
        "blank-name",
    ],
)
def test_invalid_spec_is_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="model video"):
        dataclasses.replace(FAKE_ALPHA, **overrides)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- supports()
@pytest.mark.parametrize("ratio", ["16:9", "9:16", "1:1"])
@pytest.mark.parametrize("duration", [5, 10])
def test_mock_supports_every_allowed_combination(ratio: str, duration: int) -> None:
    assert MOCK_VIDEO_MODEL.supports(ratio, duration)
    assert MOCK_VIDEO_MODEL.supports(AspectRatio(ratio), duration)


@pytest.mark.parametrize(
    ("ratio", "duration"),
    [("4:3", 5), ("16:9", 7), ("16:9", 0), ("", 5), ("16:9", True), (" 16:9", 5), ("1:1", -5)],
    ids=["ratio-4:3", "duration-7", "duration-0", "empty-ratio", "bool", "padded", "negative"],
)
def test_mock_rejects_unsupported_combination(ratio: str, duration: int) -> None:
    assert MOCK_VIDEO_MODEL.supports(ratio, duration) is False


def test_supports_follows_the_spec_not_the_global_sets() -> None:
    assert FAKE_ALPHA.supports("16:9", 5)
    assert not FAKE_ALPHA.supports("16:9", 10)  # 10 giây hợp lệ toàn cục, nhưng model không hỗ trợ
    assert not FAKE_ALPHA.supports("1:1", 5)


# --------------------------------------------------------------------------- dựng danh mục
def test_default_catalog_enables_mock() -> None:
    catalog = build_video_model_catalog(["mock"])

    assert catalog.enabled() == (MOCK_VIDEO_MODEL,)


def test_enabled_models_keep_configuration_order() -> None:
    catalog = build_video_model_catalog(["fake-beta", "mock", "fake-alpha"], known=TEST_KNOWN)

    assert [spec.key for spec in catalog.enabled()] == ["fake-beta", "mock", "fake-alpha"]


def test_disabled_known_model_is_hidden_but_still_known() -> None:
    catalog = build_video_model_catalog(["mock"], known=TEST_KNOWN)

    assert catalog.get_enabled("mock") is MOCK_VIDEO_MODEL
    assert catalog.get_enabled("fake-alpha") is None
    assert catalog.get_known("fake-alpha") is FAKE_ALPHA  # video cũ của model đã tắt vẫn tra được
    assert catalog.get_known("does-not-exist") is None
    assert catalog.get_enabled("does-not-exist") is None


def test_lookup_is_by_key_not_display_name() -> None:
    catalog = build_video_model_catalog(["mock"])

    assert catalog.get_enabled(MOCK_VIDEO_MODEL.display_name) is None
    assert catalog.get_known("MOCK") is None


def test_enabled_returns_an_immutable_tuple() -> None:
    assert isinstance(build_video_model_catalog(["mock"]).enabled(), tuple)


@pytest.mark.parametrize(
    ("enabled_keys", "message"),
    [([], "rỗng"), (["mock", "mock"], "trùng")],
    ids=["empty", "duplicate"],
)
def test_empty_or_duplicate_enabled_keys_are_rejected(
    enabled_keys: list[str], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        build_video_model_catalog(enabled_keys)


@pytest.mark.parametrize(
    "enabled_keys",
    [["mock", "sora"], ["fake-alpha"]],  # fake-alpha chỉ có trong TEST_KNOWN, không có thật
    ids=["unknown", "known-only-in-tests"],
)
def test_unknown_enabled_keys_are_rejected(enabled_keys: list[str]) -> None:
    with pytest.raises(UnknownVideoModelError, match=enabled_keys[-1]):
        build_video_model_catalog(enabled_keys)


def test_known_models_with_duplicate_keys_are_rejected() -> None:
    with pytest.raises(ValueError, match="trùng"):
        build_video_model_catalog(["mock"], known=(MOCK_VIDEO_MODEL, MOCK_VIDEO_MODEL))
