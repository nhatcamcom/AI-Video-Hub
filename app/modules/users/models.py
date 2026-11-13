from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, Enum, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, IdType, UTCDateTime
from app.core.time import utc_now

EMAIL_MAX_LENGTH = 320  # RFC 5321: 64 (local) + 1 (@) + 255 (domain)
FULL_NAME_MAX_LENGTH = 100
PASSWORD_HASH_MAX_LENGTH = 255


class UserRole(StrEnum):
    USER = "user"
    ADMIN = "admin"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(IdType, primary_key=True)
    # Luôn lưu dạng đã chuẩn hóa (chữ thường)
    # → ràng buộc UNIQUE chặn trùng không phân biệt hoa/thường
    email: Mapped[str] = mapped_column(String(EMAIL_MAX_LENGTH), unique=True)
    password_hash: Mapped[str] = mapped_column(String(PASSWORD_HASH_MAX_LENGTH))
    full_name: Mapped[str] = mapped_column(String(FULL_NAME_MAX_LENGTH))
    role: Mapped[UserRole] = mapped_column(
        Enum(
            UserRole,
            name="user_role",
            native_enum=False,
            create_constraint=True,
            length=20,
            values_callable=lambda roles: [role.value for role in roles],
            validate_strings=True,
        ),
        default=UserRole.USER,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)
