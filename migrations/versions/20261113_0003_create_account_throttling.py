"""create trusted_devices, auth_throttles

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-13

Chỉ thêm bảng mới, không đụng bảng/dữ liệu đã có → downgrade chỉ xóa hai bảng này.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ID_TYPE = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "auth_throttles",
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("allowed_at_ms", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("key_hash", name=op.f("pk_auth_throttles")),
    )
    op.create_index(op.f("ix_auth_throttles_allowed_at_ms"), "auth_throttles", ["allowed_at_ms"])
    op.create_table(
        "trusted_devices",
        sa.Column("id", ID_TYPE, nullable=False),
        sa.Column("user_id", ID_TYPE, nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_trusted_devices_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trusted_devices")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_trusted_devices_token_hash")),
    )
    op.create_index(op.f("ix_trusted_devices_expires_at"), "trusted_devices", ["expires_at"])
    op.create_index(op.f("ix_trusted_devices_user_id"), "trusted_devices", ["user_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_trusted_devices_user_id"), table_name="trusted_devices")
    op.drop_index(op.f("ix_trusted_devices_expires_at"), table_name="trusted_devices")
    op.drop_table("trusted_devices")
    op.drop_index(op.f("ix_auth_throttles_allowed_at_ms"), table_name="auth_throttles")
    op.drop_table("auth_throttles")
