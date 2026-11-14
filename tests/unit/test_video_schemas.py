"""Schema yêu cầu tạo video (P2-6): prompt, tỷ lệ, thời lượng, model, ảnh đầu vào."""

from typing import Any

import pytest
from pydantic import ValidationError

from app.modules.videos.catalog import AspectRatio
from app.modules.videos.models import PROMPT_MAX_LENGTH
from app.modules.videos.schemas import VideoCreate, VideoResponse

pytestmark = pytest.mark.unit

VALID: dict[str, Any] = {
    "prompt": "Một chú mèo đi dạo trên bãi biển",
    "aspect_ratio": "16:9",
    "duration_seconds": 5,
    "model_key": "mock",
}


def build(**overrides: Any) -> VideoCreate:
    return VideoCreate.model_validate({**VALID, **overrides})


def test_valid_request_is_parsed() -> None:
    request = build(input_image_id=12)

    assert request.prompt == VALID["prompt"]
    assert request.aspect_ratio is AspectRatio.LANDSCAPE
    assert request.duration_seconds == 5
    assert request.model_key == "mock"
    assert request.input_image_id == 12


def test_input_image_is_optional() -> None:
    assert build().input_image_id is None
    assert build(input_image_id=None).input_image_id is None


def test_prompt_limit_is_1000_characters() -> None:
    assert PROMPT_MAX_LENGTH == 1000


def test_prompt_is_stripped() -> None:
    assert build(prompt="  biển xanh \n").prompt == "biển xanh"


@pytest.mark.parametrize(
    "prompt",
    ["a" * PROMPT_MAX_LENGTH, "ệ" * PROMPT_MAX_LENGTH, "🐱" * PROMPT_MAX_LENGTH],
    ids=["ascii", "vietnamese", "emoji"],
)
def test_prompt_of_exactly_1000_code_points_is_accepted(prompt: str) -> None:
    assert len(build(prompt=prompt).prompt) == PROMPT_MAX_LENGTH


@pytest.mark.parametrize(
    "prompt",
    ["", "   ", "\n\t ", "a" * (PROMPT_MAX_LENGTH + 1), "🐱" * (PROMPT_MAX_LENGTH + 1)],
    ids=["empty", "spaces", "whitespace", "1001-ascii", "1001-emoji"],
)
def test_empty_or_too_long_prompt_is_rejected(prompt: str) -> None:
    with pytest.raises(ValidationError):
        build(prompt=prompt)


def test_padding_does_not_count_towards_the_limit() -> None:
    assert len(build(prompt=" " + "a" * PROMPT_MAX_LENGTH + " ").prompt) == PROMPT_MAX_LENGTH


@pytest.mark.parametrize(
    "control",
    ["\x00", "\x01", "\x07", "\x08", "\x0b", "\x0c", "\x1b", "\x1f"],
    ids=["nul", "soh", "bel", "bs", "vt", "ff", "esc", "us"],
)
def test_prompt_with_c0_control_character_is_rejected(control: str) -> None:
    with pytest.raises(ValidationError, match="ký tự điều khiển"):
        build(prompt=f"mèo{control}chó")


def test_prompt_may_contain_newline_tab_and_carriage_return() -> None:
    assert build(prompt="dòng 1\ndòng 2\r\n\tthụt lề").prompt == "dòng 1\ndòng 2\r\n\tthụt lề"


@pytest.mark.parametrize("ratio", ["4:3", "16x9", "", "16:09"])
def test_unknown_aspect_ratio_is_rejected(ratio: str) -> None:
    with pytest.raises(ValidationError):
        build(aspect_ratio=ratio)


@pytest.mark.parametrize(
    "duration", ["5", True, 5.0, None], ids=["string", "bool", "float", "null"]
)
def test_duration_must_be_a_strict_integer(duration: object) -> None:
    with pytest.raises(ValidationError):
        build(duration_seconds=duration)


def test_duration_values_are_left_to_the_model_capabilities() -> None:
    # Tập {5, 10} thuộc VideoModelSpec (nguồn duy nhất); schema không lặp lại
    assert build(duration_seconds=7).duration_seconds == 7


@pytest.mark.parametrize("model_key", ["", "m" * 33])
def test_model_key_length_is_bounded(model_key: str) -> None:
    with pytest.raises(ValidationError):
        build(model_key=model_key)


@pytest.mark.parametrize(
    "image_id", [0, -1, "12", True, 1.5], ids=["zero", "neg", "str", "bool", "float"]
)
def test_input_image_id_must_be_a_positive_strict_integer(image_id: object) -> None:
    with pytest.raises(ValidationError):
        build(input_image_id=image_id)


@pytest.mark.parametrize("field", ["status", "owner_id", "active_slot", "output_storage_key"])
def test_unknown_fields_are_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        build(**{field: "x"})


@pytest.mark.parametrize("missing", ["prompt", "aspect_ratio", "duration_seconds", "model_key"])
def test_required_fields(missing: str) -> None:
    payload = {key: value for key, value in VALID.items() if key != missing}

    with pytest.raises(ValidationError):
        VideoCreate.model_validate(payload)


def test_response_exposes_no_internal_fields() -> None:
    assert set(VideoResponse.model_fields) == {
        "id",
        "status",
        "progress",
        "prompt",
        "aspect_ratio",
        "duration_seconds",
        "model_key",
        "input_image_id",
        "error_code",
        "created_at",
        "started_at",
        "finished_at",
    }
