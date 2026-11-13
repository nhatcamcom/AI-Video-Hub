from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, EmailStr, StringConstraints

from app.modules.users.models import FULL_NAME_MAX_LENGTH, UserRole


def normalize_email(email: str) -> str:
    """Email lưu và so sánh ở dạng chữ thường, không khoảng trắng hai đầu."""
    return email.strip().lower()


# Pydantic kiểm tra định dạng email (không tra DNS), sau đó chuẩn hóa
NormalizedEmail = Annotated[EmailStr, AfterValidator(normalize_email)]
FullName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=FULL_NAME_MAX_LENGTH),
]


class StrictRequest(BaseModel):
    """Request từ client: field lạ (role, is_active…) bị từ chối thay vì lặng lẽ bỏ qua."""

    model_config = ConfigDict(extra="forbid")


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    full_name: str
    role: UserRole
    is_active: bool
    email_verified: bool
    created_at: datetime
    updated_at: datetime


class UserListResponse(BaseModel):
    total: int
    items: list[UserResponse]


class UserProfileUpdate(StrictRequest):
    """Chỉ những gì người dùng được tự sửa. Email/role/trạng thái KHÔNG có ở đây."""

    full_name: FullName
