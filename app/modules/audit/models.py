from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, IdType, UTCDateTime
from app.core.time import utc_now

ACTOR_MAX_LENGTH = 255
ACTION_MAX_LENGTH = 64


class AuditEvent(Base):
    """Ai làm gì, với tài khoản nào, lúc nào. Không lưu email hay dữ liệu bí mật."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(IdType, primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, index=True)
    actor: Mapped[str] = mapped_column(String(ACTOR_MAX_LENGTH))
    action: Mapped[str] = mapped_column(String(ACTION_MAX_LENGTH))
    # SET NULL: xóa tài khoản không được xóa dấu vết đã từng cấp quyền cho nó
    target_user_id: Mapped[int | None] = mapped_column(
        IdType, ForeignKey("users.id", ondelete="SET NULL"), index=True, default=None
    )
