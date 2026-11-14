"""create user_storage_usage

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-14

P2-3: bang han muc dung luong luu tru theo user (mot hang moi user, tao khi giu cho lan dau).
Chi them bang moi, khong dung bang/du lieu da co -> downgrade chi xoa bang nay.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ID_TYPE = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "user_storage_usage",
        sa.Column("user_id", ID_TYPE, nullable=False),
        sa.Column("used_bytes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "used_bytes >= 0", name=op.f("ck_user_storage_usage_used_bytes_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_storage_usage_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_user_storage_usage")),
    )


def downgrade() -> None:
    op.drop_table("user_storage_usage")
