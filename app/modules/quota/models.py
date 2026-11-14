from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, IdType, UTCDateTime
from app.core.time import utc_now


class UserStorageUsage(Base):
    """Dung lượng đã dùng của một user: nguồn DUY NHẤT để kiểm tra hạn mức.

    Một hàng mỗi user, tạo khi giữ chỗ lần đầu. Chỉ thay đổi bằng UPDATE có điều kiện trong
    `StorageQuotaService`; ràng buộc CHECK là lớp bảo vệ cuối cùng chống giá trị âm.
    """

    __tablename__ = "user_storage_usage"
    __table_args__ = (CheckConstraint("used_bytes >= 0", name="used_bytes_non_negative"),)

    # Khóa chính cũng là khóa ngoại → đã có index, không cần index riêng
    user_id: Mapped[int] = mapped_column(
        IdType, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    used_bytes: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
