from datetime import datetime

from pydantic import BaseModel, ConfigDict


class MediaImageResponse(BaseModel):
    """Metadata công khai của ảnh. KHÔNG chứa storage_key, sha256 hay đường dẫn trên đĩa."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    content_type: str
    size_bytes: int
    width: int
    height: int
    created_at: datetime
