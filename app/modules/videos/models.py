"""Bảng `videos` (P2-6): yêu cầu tạo video, đồng thời là hàng đợi bền cho worker (P2-7).

Giới hạn "tối đa 2 job đang chạy mỗi user" do DATABASE bảo đảm, không phải đếm ở tầng ứng dụng:
- Job `queued` / `processing` giữ một `active_slot` (1 hoặc 2); job đã kết thúc có slot NULL.
- UNIQUE `(owner_id, active_slot)`: SQLite và PostgreSQL đều coi các NULL là khác nhau, nên chỉ slot
  đang dùng mới bị ràng buộc → mỗi user tối đa 2 dòng có slot, kể cả khi nhiều request đồng thời.
- CHECK `active_slot_matches_status`: slot có giá trị ⇔ trạng thái đang chạy. Worker trả slot trong
  CÙNG câu UPDATE chuyển sang trạng thái kết thúc, nên không có bộ đếm nào có thể lệch.

CHECK được dựng từ hằng số của catalog / schema để ORM luôn khớp code; migration 0006 ghi cố định
cùng giá trị (migration không được phụ thuộc code có thể thay đổi sau này).
"""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, IdType, UTCDateTime
from app.core.time import utc_now
from app.modules.videos.catalog import ALLOWED_DURATIONS_SECONDS, AspectRatio


class VideoStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


ACTIVE_STATUSES: tuple[VideoStatus, ...] = (VideoStatus.QUEUED, VideoStatus.PROCESSING)
FINISHED_STATUSES: tuple[VideoStatus, ...] = (VideoStatus.SUCCEEDED, VideoStatus.FAILED)
MAX_ACTIVE_VIDEOS_PER_USER = 2  # quyết định 7.5; đổi con số này phải kèm migration (CHECK slot)
ACTIVE_SLOTS: tuple[int, ...] = tuple(range(1, MAX_ACTIVE_VIDEOS_PER_USER + 1))
ACTIVE_SLOT_CONSTRAINT_NAME = "uq_videos_owner_id_active_slot"
PROMPT_MAX_LENGTH = 1000
MODEL_KEY_MAX_LENGTH = 32
STORAGE_KEY_MAX_LENGTH = 64
ERROR_CODE_MAX_LENGTH = 64
STATUS_MAX_LENGTH = 16
ASPECT_RATIO_MAX_LENGTH = 8
LEASE_TOKEN_LENGTH = 32  # 128 bit ngẫu nhiên dạng hex (P2-7)


def _sql_list(values: tuple[object, ...]) -> str:
    return ", ".join(f"'{value}'" if isinstance(value, str) else str(value) for value in values)


_ACTIVE_SQL = _sql_list(ACTIVE_STATUSES)
_FINISHED_SQL = _sql_list(FINISHED_STATUSES)


def _file_consistent(prefix: str) -> str:
    return (
        f"({prefix}_storage_key IS NULL AND {prefix}_size_bytes IS NULL) OR "
        # IS NOT NULL bắt buộc: "NULL > 0" là NULL và CHECK coi NULL là ĐẠT
        f"({prefix}_storage_key IS NOT NULL AND {prefix}_size_bytes IS NOT NULL"
        f" AND {prefix}_size_bytes > 0)"
    )


class Video(Base):
    __tablename__ = "videos"
    __table_args__ = (
        CheckConstraint(f"length(prompt) BETWEEN 1 AND {PROMPT_MAX_LENGTH}", name="prompt_length"),
        CheckConstraint(
            f"aspect_ratio IN ({_sql_list(tuple(AspectRatio))})", name="aspect_ratio_allowed"
        ),
        CheckConstraint(
            f"duration_seconds IN ({_sql_list(ALLOWED_DURATIONS_SECONDS)})", name="duration_allowed"
        ),
        CheckConstraint(f"status IN ({_sql_list(tuple(VideoStatus))})", name="status_allowed"),
        CheckConstraint(f"active_slot IN ({_sql_list(ACTIVE_SLOTS)})", name="active_slot_allowed"),
        CheckConstraint(
            f"(status IN ({_ACTIVE_SQL}) AND active_slot IS NOT NULL) OR "
            f"(status IN ({_FINISHED_SQL}) AND active_slot IS NULL)",
            name="active_slot_matches_status",
        ),
        CheckConstraint("progress BETWEEN 0 AND 100", name="progress_range"),
        CheckConstraint(_file_consistent("output"), name="output_file_consistent"),
        CheckConstraint(_file_consistent("poster"), name="poster_file_consistent"),
        # P2-7: job đang xử lý ⇔ có lease (token + hạn); mọi trạng thái khác không có lease
        CheckConstraint("attempt_count >= 0", name="attempt_count_non_negative"),
        CheckConstraint(
            f"(status = '{VideoStatus.PROCESSING}' AND lease_token IS NOT NULL"
            " AND lease_expires_at IS NOT NULL) OR "
            f"(status <> '{VideoStatus.PROCESSING}' AND lease_token IS NULL"
            " AND lease_expires_at IS NULL)",
            name="lease_matches_status",
        ),
        # Reaper tìm job `processing` đã hết lease
        Index("ix_videos_status_lease_expires_at", "status", "lease_expires_at"),
        # Đặt tên tường minh: quy ước "uq" chỉ lấy cột đầu → tên mặc định dễ gây nhầm. Service dùng
        # đúng tên này để nhận diện tranh chấp slot trên PostgreSQL
        UniqueConstraint("owner_id", "active_slot", name=ACTIVE_SLOT_CONSTRAINT_NAME),
        # Thư viện của user (lọc owner_id, sắp theo created_at); index này cũng phục vụ FK owner_id
        Index("ix_videos_owner_id_created_at", "owner_id", "created_at"),
        Index("ix_videos_status_created_at", "status", "created_at"),  # worker lấy job (P2-7)
    )

    id: Mapped[int] = mapped_column(IdType, primary_key=True)
    owner_id: Mapped[int] = mapped_column(IdType, ForeignKey("users.id", ondelete="CASCADE"))
    prompt: Mapped[str] = mapped_column(Text)
    input_image_id: Mapped[int | None] = mapped_column(
        IdType, ForeignKey("media_assets.id", ondelete="SET NULL"), index=True, nullable=True
    )
    aspect_ratio: Mapped[str] = mapped_column(String(ASPECT_RATIO_MAX_LENGTH))
    duration_seconds: Mapped[int] = mapped_column(Integer)
    # Key model bất biến (P2-5), không phải display_name
    model_key: Mapped[str] = mapped_column(String(MODEL_KEY_MAX_LENGTH))
    status: Mapped[str] = mapped_column(String(STATUS_MAX_LENGTH), default=VideoStatus.QUEUED)
    active_slot: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    progress: Mapped[int] = mapped_column(SmallInteger, default=0)
    error_code: Mapped[str | None] = mapped_column(String(ERROR_CODE_MAX_LENGTH), nullable=True)
    output_storage_key: Mapped[str | None] = mapped_column(
        String(STORAGE_KEY_MAX_LENGTH), unique=True, nullable=True
    )
    output_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    poster_storage_key: Mapped[str | None] = mapped_column(
        String(STORAGE_KEY_MAX_LENGTH), unique=True, nullable=True
    )
    poster_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Lease của worker (P2-7): chỉ worker giữ đúng token mới được gia hạn / kết thúc job
    lease_token: Mapped[str | None] = mapped_column(String(LEASE_TOKEN_LENGTH), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    attempt_count: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
