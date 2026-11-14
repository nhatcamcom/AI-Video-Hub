"""create videos

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-15

P2-6: bang videos (yeu cau tao video + hang doi ben cho worker P2-7).
Toi da 2 job dang chay moi user do database bao dam: active_slot (1 hoac 2) chi co gia tri khi
status la queued/processing, UNIQUE (owner_id, active_slot) chan slot thu 3.
Chi them bang moi, khong dung bang/du lieu da co -> downgrade chi xoa bang nay.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ID_TYPE = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "videos",
        sa.Column("id", ID_TYPE, nullable=False),
        sa.Column("owner_id", ID_TYPE, nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("input_image_id", ID_TYPE, nullable=True),
        sa.Column("aspect_ratio", sa.String(length=8), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=False),
        sa.Column("model_key", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("active_slot", sa.SmallInteger(), nullable=True),
        sa.Column("progress", sa.SmallInteger(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("output_storage_key", sa.String(length=64), nullable=True),
        sa.Column("output_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("poster_storage_key", sa.String(length=64), nullable=True),
        sa.Column("poster_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(prompt) BETWEEN 1 AND 1000", name=op.f("ck_videos_prompt_length")
        ),
        sa.CheckConstraint(
            "aspect_ratio IN ('16:9', '9:16', '1:1')", name=op.f("ck_videos_aspect_ratio_allowed")
        ),
        sa.CheckConstraint("duration_seconds IN (5, 10)", name=op.f("ck_videos_duration_allowed")),
        sa.CheckConstraint(
            "status IN ('queued', 'processing', 'succeeded', 'failed')",
            name=op.f("ck_videos_status_allowed"),
        ),
        sa.CheckConstraint("active_slot IN (1, 2)", name=op.f("ck_videos_active_slot_allowed")),
        sa.CheckConstraint(
            "(status IN ('queued', 'processing') AND active_slot IS NOT NULL) OR "
            "(status IN ('succeeded', 'failed') AND active_slot IS NULL)",
            name=op.f("ck_videos_active_slot_matches_status"),
        ),
        sa.CheckConstraint("progress BETWEEN 0 AND 100", name=op.f("ck_videos_progress_range")),
        sa.CheckConstraint(
            "(output_storage_key IS NULL AND output_size_bytes IS NULL) OR "
            "(output_storage_key IS NOT NULL AND output_size_bytes IS NOT NULL"
            " AND output_size_bytes > 0)",
            name=op.f("ck_videos_output_file_consistent"),
        ),
        sa.CheckConstraint(
            "(poster_storage_key IS NULL AND poster_size_bytes IS NULL) OR "
            "(poster_storage_key IS NOT NULL AND poster_size_bytes IS NOT NULL"
            " AND poster_size_bytes > 0)",
            name=op.f("ck_videos_poster_file_consistent"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_videos_owner_id_users"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["input_image_id"],
            ["media_assets.id"],
            name=op.f("fk_videos_input_image_id_media_assets"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_videos")),
        sa.UniqueConstraint("owner_id", "active_slot", name=op.f("uq_videos_owner_id_active_slot")),
        sa.UniqueConstraint("output_storage_key", name=op.f("uq_videos_output_storage_key")),
        sa.UniqueConstraint("poster_storage_key", name=op.f("uq_videos_poster_storage_key")),
    )
    # Bang moi, rong -> tao index thuong la du (khong can CONCURRENTLY)
    op.create_index("ix_videos_owner_id_created_at", "videos", ["owner_id", "created_at"])
    op.create_index("ix_videos_status_created_at", "videos", ["status", "created_at"])
    op.create_index("ix_videos_input_image_id", "videos", ["input_image_id"])


def downgrade() -> None:
    op.drop_index("ix_videos_input_image_id", table_name="videos")
    op.drop_index("ix_videos_status_created_at", table_name="videos")
    op.drop_index("ix_videos_owner_id_created_at", table_name="videos")
    op.drop_table("videos")
