from pydantic import BaseModel, ConfigDict

from app.modules.videos.catalog import AspectRatio


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
