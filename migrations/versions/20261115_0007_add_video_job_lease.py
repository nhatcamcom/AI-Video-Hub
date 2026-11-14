"""add video job lease

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-15

P2-7: lease va retry cho worker tao video.
- lease_token / lease_expires_at: chi worker giu dung token moi duoc gia han / ket thuc job.
- attempt_count: chi tang khi claim thanh cong. next_attempt_at: backoff khi thu lai.
- CHECK lease_matches_status: processing <=> co lease (token + han).
SQLite khong ALTER duoc CHECK -> batch_alter_table (tao lai bang, giu nguyen rang buoc cu; engine
migration da tat PRAGMA foreign_keys nen khong kich hoat CASCADE). Khong tu sua du lieu: neu co dong
processing sua tay (chua tung co worker) thi CHECK lam migration dung lai ro rang.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEASE_CHECK = (
    "(status = 'processing' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
    "(status <> 'processing' AND lease_token IS NULL AND lease_expires_at IS NULL)"
)


def upgrade() -> None:
    with op.batch_alter_table("videos") as batch:
        batch.add_column(sa.Column("lease_token", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(
            sa.Column("attempt_count", sa.SmallInteger(), server_default="0", nullable=False)
        )
        batch.add_column(sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_check_constraint(
            op.f("ck_videos_attempt_count_non_negative"), "attempt_count >= 0"
        )
        batch.create_check_constraint(op.f("ck_videos_lease_matches_status"), LEASE_CHECK)
        batch.create_index(
            "ix_videos_status_lease_expires_at", ["status", "lease_expires_at"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("videos") as batch:
        batch.drop_index("ix_videos_status_lease_expires_at")
        batch.drop_constraint(op.f("ck_videos_lease_matches_status"), type_="check")
        batch.drop_constraint(op.f("ck_videos_attempt_count_non_negative"), type_="check")
        batch.drop_column("next_attempt_at")
        batch.drop_column("attempt_count")
        batch.drop_column("lease_expires_at")
        batch.drop_column("lease_token")
