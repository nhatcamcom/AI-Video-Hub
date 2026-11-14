from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, IdType, UTCDateTime
from app.core.time import utc_now

STORAGE_KEY_MAX_LENGTH = 64  # "uploads/" + 32 hex + đuôi ≤ 5 ký tự = 45
CONTENT_TYPE_MAX_LENGTH = 32
SHA256_HEX_LENGTH = 64


class MediaAsset(Base):
    """Ảnh đầu vào đã kiểm tra và mã hóa lại; file nằm ở `storage_key` (khóa do server sinh).

    `size_bytes` là số byte THỰC của file đã lưu — đúng bằng phần đã giữ chỗ trong hạn mức.
    `deleted_at` dành cho bước xóa / dọn dẹp sau này (P2-4 chưa có API xóa).
    """

    __tablename__ = "media_assets"
    __table_args__ = (
        CheckConstraint(
            "content_type IN ('image/png', 'image/jpeg', 'image/webp')",
            name="content_type_allowed",
        ),
        CheckConstraint("size_bytes > 0", name="size_bytes_positive"),
        CheckConstraint("width > 0", name="width_positive"),
        CheckConstraint("height > 0", name="height_positive"),
        # Cột so sánh bằng (owner_id) trước, cột khoảng / sắp xếp (created_at) sau; index này cũng
        # phục vụ khóa ngoại owner_id nên không cần index riêng cho nó
        Index("ix_media_assets_owner_id_created_at", "owner_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(IdType, primary_key=True)
    owner_id: Mapped[int] = mapped_column(IdType, ForeignKey("users.id", ondelete="CASCADE"))
    storage_key: Mapped[str] = mapped_column(String(STORAGE_KEY_MAX_LENGTH), unique=True)
    content_type: Mapped[str] = mapped_column(String(CONTENT_TYPE_MAX_LENGTH))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(SHA256_HEX_LENGTH))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
