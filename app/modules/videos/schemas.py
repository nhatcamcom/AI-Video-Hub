import re
from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictInt, StringConstraints

from app.modules.users.schemas import StrictRequest
from app.modules.videos.catalog import AspectRatio
from app.modules.videos.models import MODEL_KEY_MAX_LENGTH, PROMPT_MAX_LENGTH, VideoStatus

# Prompt tối đa PROMPT_MAX_LENGTH (quyết định 7.5) code point, đếm SAU khi bỏ khoảng trắng hai đầu.
# Ký tự điều khiển C0 trừ tab, xuống dòng (LF, CR). NUL còn bị PostgreSQL từ chối trong cột TEXT
_FORBIDDEN_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _reject_control_characters(prompt: str) -> str:
    if _FORBIDDEN_CONTROL_CHARACTERS.search(prompt):
        raise ValueError("prompt không được chứa ký tự điều khiển")
    return prompt


Prompt = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=PROMPT_MAX_LENGTH),
    AfterValidator(_reject_control_characters),
]
ModelKey = Annotated[str, StringConstraints(min_length=1, max_length=MODEL_KEY_MAX_LENGTH)]
ImageId = Annotated[int, Field(ge=1, strict=True)]


class VideoModelResponse(BaseModel):
    """Đúng 6 trường công khai, đọc thẳng từ VideoModelSpec (không có cấu hình hay tên provider)."""

    model_config = ConfigDict(from_attributes=True)

    key: str
    display_name: str
    is_demo: bool
    aspect_ratios: list[AspectRatio]
    durations_seconds: list[int]
    supports_input_image: bool


class VideoModelListResponse(BaseModel):
    total: int
    items: list[VideoModelResponse]


class VideoCreate(StrictRequest):
    """Yêu cầu tạo video. Tỷ lệ / thời lượng / ảnh đầu vào có hợp lệ với model hay không do
    `VideoModelSpec` quyết định ở service (nguồn duy nhất), schema chỉ kiểm tra kiểu dữ liệu."""

    prompt: Prompt
    aspect_ratio: AspectRatio
    duration_seconds: StrictInt
    model_key: ModelKey
    input_image_id: ImageId | None = None


class VideoResponse(BaseModel):
    """Video của chính user. KHÔNG có storage key, kích thước file hay slot nội bộ."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    status: VideoStatus
    progress: int
    prompt: str
    aspect_ratio: AspectRatio
    duration_seconds: int
    model_key: str
    input_image_id: int | None
    error_code: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
