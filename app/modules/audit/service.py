import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_now
from app.modules.audit.models import AuditEvent

logger = logging.getLogger("app.audit")

ADMIN_ROLE_GRANTED = "user.role.admin_granted"


class AuditService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def record(self, *, actor: str, action: str, target_user_id: int | None) -> AuditEvent:
        """Thêm sự kiện vào transaction hiện tại; người gọi commit rồi gọi `log_committed`.

        Ghi chung transaction đảm bảo không có thay đổi quyền nào mà thiếu dấu vết audit.
        """
        event = AuditEvent(
            occurred_at=utc_now(), actor=actor, action=action, target_user_id=target_user_id
        )
        self.db.add(event)
        return event

    @staticmethod
    def log_committed(event: AuditEvent) -> None:
        """Gọi SAU khi commit thành công: log không bao giờ khẳng định một thay đổi đã rollback.

        Chỉ ghi id, không ghi email (PII).
        """
        logger.info(
            "audit action=%s actor=%s target_user_id=%s",
            event.action,
            event.actor,
            event.target_user_id,
        )
