"""create media_assets

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-15

P2-4: bang anh dau vao da kiem tra va ma hoa lai (file nam o storage_key do server sinh).
Chi them bang moi, khong dung bang/du lieu da co -> downgrade chi xoa bang nay.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ID_TYPE = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "media_assets",
        sa.Column("id", ID_TYPE, nullable=False),
        sa.Column("owner_id", ID_TYPE, nullable=False),
        sa.Column("storage_key", sa.String(length=64), nullable=False),
        sa.Column("content_type", sa.String(length=32), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "content_type IN ('image/png', 'image/jpeg', 'image/webp')",
            name=op.f("ck_media_assets_content_type_allowed"),
        ),
        sa.CheckConstraint("size_bytes > 0", name=op.f("ck_media_assets_size_bytes_positive")),
        sa.CheckConstraint("width > 0", name=op.f("ck_media_assets_width_positive")),
        sa.CheckConstraint("height > 0", name=op.f("ck_media_assets_height_positive")),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_media_assets_owner_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_assets")),
        sa.UniqueConstraint("storage_key", name=op.f("uq_media_assets_storage_key")),
    )
    # Bang moi, rong -> tao index thuong la du (khong can CONCURRENTLY)
    op.create_index(
        "ix_media_assets_owner_id_created_at",
        "media_assets",
        ["owner_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_media_assets_owner_id_created_at", table_name="media_assets")
    op.drop_table("media_assets")
