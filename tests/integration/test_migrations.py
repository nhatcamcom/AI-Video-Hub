"""Migration Alembic: chạy được lên/xuống và khớp đúng với ORM model."""

from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

import app.model_registry  # noqa: F401  — nạp mọi model vào metadata
from app.core.database import Base

pytestmark = pytest.mark.integration

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAIN_DATABASE = PROJECT_ROOT / "ai_video_hub.db"
AUTH_TABLES = {"users", "user_sessions", "password_reset_tokens"}
QUOTA_TABLE = "user_storage_usage"
MEDIA_TABLE = "media_assets"
VIDEOS_TABLE = "videos"


def verify_temporary_database(database_path: Path) -> Path:
    """Dừng ngay nếu migration sắp chạy trên bất kỳ DB nào ngoài DB tạm của test."""
    resolved = database_path.resolve()
    if resolved == MAIN_DATABASE.resolve() or resolved.name == MAIN_DATABASE.name:
        pytest.fail(f"DỪNG: migration test trỏ tới DB chính {resolved}")
    if resolved.is_relative_to(PROJECT_ROOT.resolve()):
        pytest.fail(f"DỪNG: DB test nằm trong thư mục dự án {resolved}")
    return resolved


def alembic_config(database_path: Path) -> Config:
    verify_temporary_database(database_path)
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path.as_posix()}")
    config.attributes["configure_logger"] = False  # không đè cấu hình logging của pytest
    return config


def table_names(database_path: Path) -> set[str]:
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_upgrade_creates_auth_tables(tmp_path: Path) -> None:
    database_path = tmp_path / "migrate.db"

    command.upgrade(alembic_config(database_path), "head")

    assert table_names(database_path) >= AUTH_TABLES


def test_migrated_schema_matches_orm_models(tmp_path: Path) -> None:
    database_path = tmp_path / "migrate.db"
    command.upgrade(alembic_config(database_path), "head")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            differences = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()

    assert differences == []


def test_upgrade_creates_audit_table(tmp_path: Path) -> None:
    database_path = tmp_path / "migrate.db"

    command.upgrade(alembic_config(database_path), "head")

    assert "audit_events" in table_names(database_path)


def test_downgrade_to_0001_removes_only_the_audit_table(tmp_path: Path) -> None:
    database_path = tmp_path / "migrate.db"
    config = alembic_config(database_path)
    command.upgrade(config, "head")

    command.downgrade(config, "0001")

    tables = table_names(database_path)
    assert "audit_events" not in tables
    assert table_names(database_path) >= AUTH_TABLES


def test_migration_guard_rejects_main_database() -> None:
    with pytest.raises(pytest.fail.Exception, match="DỪNG"):
        verify_temporary_database(MAIN_DATABASE)


def test_migration_runs_on_the_given_temporary_database(tmp_path: Path) -> None:
    database_path = tmp_path / "migrate.db"

    command.upgrade(alembic_config(database_path), "head")

    assert database_path.is_file()  # đúng file tạm được tạo và nhận schema
    assert QUOTA_TABLE in table_names(database_path)


def test_revision_0004_follows_0003() -> None:
    scripts = ScriptDirectory.from_config(Config(str(PROJECT_ROOT / "alembic.ini")))

    revision = scripts.get_revision("0004")

    assert revision is not None
    assert revision.down_revision == "0003"


def test_revision_0005_follows_0004() -> None:
    scripts = ScriptDirectory.from_config(Config(str(PROJECT_ROOT / "alembic.ini")))

    revision = scripts.get_revision("0005")

    assert revision is not None
    assert revision.down_revision == "0004"


def test_revision_0006_follows_0005() -> None:
    scripts = ScriptDirectory.from_config(Config(str(PROJECT_ROOT / "alembic.ini")))

    revision = scripts.get_revision("0006")

    assert revision is not None
    assert revision.down_revision == "0005"


def test_revision_0007_follows_0006_and_is_head() -> None:
    scripts = ScriptDirectory.from_config(Config(str(PROJECT_ROOT / "alembic.ini")))

    revision = scripts.get_revision("0007")

    assert revision is not None
    assert revision.down_revision == "0006"
    assert scripts.get_current_head() == "0007"


def test_upgrade_creates_storage_usage_table_with_constraints(tmp_path: Path) -> None:
    database_path = tmp_path / "migrate.db"
    command.upgrade(alembic_config(database_path), "head")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        inspector = inspect(engine)
        columns = {column["name"] for column in inspector.get_columns(QUOTA_TABLE)}
        primary_key = inspector.get_pk_constraint(QUOTA_TABLE)["constrained_columns"]
        foreign_keys = inspector.get_foreign_keys(QUOTA_TABLE)
        checks = [check["name"] for check in inspector.get_check_constraints(QUOTA_TABLE)]
    finally:
        engine.dispose()

    assert columns == {"user_id", "used_bytes", "updated_at"}
    assert primary_key == ["user_id"]
    assert foreign_keys[0]["referred_table"] == "users"
    assert foreign_keys[0]["options"].get("ondelete") == "CASCADE"
    assert checks == ["ck_user_storage_usage_used_bytes_non_negative"]


def test_downgrade_to_0003_removes_only_the_storage_usage_table(tmp_path: Path) -> None:
    database_path = tmp_path / "migrate.db"
    config = alembic_config(database_path)
    command.upgrade(config, "head")
    before = table_names(database_path)

    command.downgrade(config, "0003")

    after = table_names(database_path)
    assert before - after == {QUOTA_TABLE, MEDIA_TABLE, VIDEOS_TABLE}  # 0005, 0006 cũng bị gỡ
    assert after >= AUTH_TABLES | {"audit_events", "auth_throttles", "trusted_devices"}


def test_upgrade_after_downgrade_recreates_storage_usage_table(tmp_path: Path) -> None:
    database_path = tmp_path / "migrate.db"
    config = alembic_config(database_path)
    command.upgrade(config, "head")
    command.downgrade(config, "0003")

    command.upgrade(config, "head")

    assert QUOTA_TABLE in table_names(database_path)


def test_downgrade_removes_auth_tables(tmp_path: Path) -> None:
    database_path = tmp_path / "migrate.db"
    config = alembic_config(database_path)
    command.upgrade(config, "head")

    command.downgrade(config, "base")

    assert AUTH_TABLES.isdisjoint(table_names(database_path))


def test_upgrade_creates_media_assets_table_with_constraints(tmp_path: Path) -> None:
    database_path = tmp_path / "migrate.db"
    command.upgrade(alembic_config(database_path), "head")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        inspector = inspect(engine)
        columns = {column["name"]: column for column in inspector.get_columns(MEDIA_TABLE)}
        foreign_keys = inspector.get_foreign_keys(MEDIA_TABLE)
        checks = {check["name"] for check in inspector.get_check_constraints(MEDIA_TABLE)}
        uniques = inspector.get_unique_constraints(MEDIA_TABLE)
        indexes = {
            index["name"]: index["column_names"] for index in inspector.get_indexes(MEDIA_TABLE)
        }
    finally:
        engine.dispose()

    assert set(columns) == {
        "id", "owner_id", "storage_key", "content_type", "size_bytes",
        "width", "height", "sha256", "created_at", "deleted_at",
    }  # fmt: skip
    nullable = {name for name, column in columns.items() if column["nullable"]}
    assert nullable == {"deleted_at"}  # width/height NOT NULL
    assert foreign_keys[0]["referred_table"] == "users"
    assert foreign_keys[0]["constrained_columns"] == ["owner_id"]
    assert foreign_keys[0]["options"].get("ondelete") == "CASCADE"
    assert checks == {
        "ck_media_assets_content_type_allowed",
        "ck_media_assets_size_bytes_positive",
        "ck_media_assets_width_positive",
        "ck_media_assets_height_positive",
    }
    assert [unique["column_names"] for unique in uniques] == [["storage_key"]]
    assert indexes == {"ix_media_assets_owner_id_created_at": ["owner_id", "created_at"]}


def test_downgrade_to_0004_removes_only_the_media_assets_table(tmp_path: Path) -> None:
    database_path = tmp_path / "migrate.db"
    config = alembic_config(database_path)
    command.upgrade(config, "head")
    before = table_names(database_path)

    command.downgrade(config, "0004")

    after = table_names(database_path)
    assert before - after == {MEDIA_TABLE, VIDEOS_TABLE}  # 0006 phụ thuộc 0005 nên cũng bị gỡ
    assert QUOTA_TABLE in after


def test_upgrade_after_downgrade_recreates_media_assets_table(tmp_path: Path) -> None:
    database_path = tmp_path / "migrate.db"
    config = alembic_config(database_path)
    command.upgrade(config, "head")
    command.downgrade(config, "0004")

    command.upgrade(config, "head")

    assert MEDIA_TABLE in table_names(database_path)


def test_upgrade_creates_videos_table_with_constraints(tmp_path: Path) -> None:
    database_path = tmp_path / "migrate.db"
    command.upgrade(alembic_config(database_path), "0006")  # schema đúng lúc tạo (P2-6)

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        inspector = inspect(engine)
        columns = {column["name"]: column for column in inspector.get_columns(VIDEOS_TABLE)}
        foreign_keys = {
            fk["name"]: (
                fk["constrained_columns"],
                fk["referred_table"],
                fk["options"].get("ondelete"),
            )
            for fk in inspector.get_foreign_keys(VIDEOS_TABLE)
        }
        checks = {check["name"] for check in inspector.get_check_constraints(VIDEOS_TABLE)}
        uniques = {
            unique["name"]: unique["column_names"]
            for unique in inspector.get_unique_constraints(VIDEOS_TABLE)
        }
        indexes = {
            index["name"]: index["column_names"] for index in inspector.get_indexes(VIDEOS_TABLE)
        }
    finally:
        engine.dispose()

    assert set(columns) == {
        "id", "owner_id", "prompt", "input_image_id", "aspect_ratio", "duration_seconds",
        "model_key", "status", "active_slot", "progress", "error_code", "output_storage_key",
        "output_size_bytes", "poster_storage_key", "poster_size_bytes", "created_at",
        "started_at", "finished_at", "deleted_at",
    }  # fmt: skip
    nullable = {name for name, column in columns.items() if column["nullable"]}
    assert nullable == {
        "input_image_id", "active_slot", "error_code", "output_storage_key", "output_size_bytes",
        "poster_storage_key", "poster_size_bytes", "started_at", "finished_at", "deleted_at",
    }  # fmt: skip
    assert foreign_keys == {
        "fk_videos_owner_id_users": (["owner_id"], "users", "CASCADE"),
        "fk_videos_input_image_id_media_assets": (["input_image_id"], "media_assets", "SET NULL"),
    }
    assert checks == {
        "ck_videos_prompt_length",
        "ck_videos_aspect_ratio_allowed",
        "ck_videos_duration_allowed",
        "ck_videos_status_allowed",
        "ck_videos_active_slot_allowed",
        "ck_videos_active_slot_matches_status",
        "ck_videos_progress_range",
        "ck_videos_output_file_consistent",
        "ck_videos_poster_file_consistent",
    }
    assert uniques == {
        "uq_videos_owner_id_active_slot": ["owner_id", "active_slot"],
        "uq_videos_output_storage_key": ["output_storage_key"],
        "uq_videos_poster_storage_key": ["poster_storage_key"],
    }
    assert indexes == {
        "ix_videos_owner_id_created_at": ["owner_id", "created_at"],
        "ix_videos_status_created_at": ["status", "created_at"],
        "ix_videos_input_image_id": ["input_image_id"],
    }


def test_downgrade_to_0005_removes_only_the_videos_table(tmp_path: Path) -> None:
    database_path = tmp_path / "migrate.db"
    config = alembic_config(database_path)
    command.upgrade(config, "head")
    before = table_names(database_path)

    command.downgrade(config, "0005")

    after = table_names(database_path)
    assert before - after == {VIDEOS_TABLE}
    assert {QUOTA_TABLE, MEDIA_TABLE} <= after


def test_upgrade_after_downgrade_recreates_videos_table(tmp_path: Path) -> None:
    database_path = tmp_path / "migrate.db"
    config = alembic_config(database_path)
    command.upgrade(config, "head")
    command.downgrade(config, "0005")

    command.upgrade(config, "head")

    assert VIDEOS_TABLE in table_names(database_path)


VIDEO_0006_CHECKS = {
    "ck_videos_prompt_length",
    "ck_videos_aspect_ratio_allowed",
    "ck_videos_duration_allowed",
    "ck_videos_status_allowed",
    "ck_videos_active_slot_allowed",
    "ck_videos_active_slot_matches_status",
    "ck_videos_progress_range",
    "ck_videos_output_file_consistent",
    "ck_videos_poster_file_consistent",
}
LEASE_COLUMNS = {"lease_token", "lease_expires_at", "attempt_count", "next_attempt_at"}


def videos_schema(database_path: Path) -> dict[str, Any]:
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        inspector = inspect(engine)
        return {
            "columns": {c["name"]: c for c in inspector.get_columns(VIDEOS_TABLE)},
            "checks": {c["name"] for c in inspector.get_check_constraints(VIDEOS_TABLE)},
            "uniques": {u["name"] for u in inspector.get_unique_constraints(VIDEOS_TABLE)},
            "indexes": {i["name"]: i["column_names"] for i in inspector.get_indexes(VIDEOS_TABLE)},
            "fks": {fk["name"] for fk in inspector.get_foreign_keys(VIDEOS_TABLE)},
        }
    finally:
        engine.dispose()


def test_upgrade_0007_adds_lease_columns_and_keeps_existing_constraints(tmp_path: Path) -> None:
    database_path = tmp_path / "migrate.db"
    command.upgrade(alembic_config(database_path), "head")

    schema = videos_schema(database_path)

    columns = schema["columns"]
    assert set(columns) >= LEASE_COLUMNS
    assert columns["attempt_count"]["nullable"] is False
    assert {"lease_token", "lease_expires_at", "next_attempt_at"} <= {
        name for name, column in columns.items() if column["nullable"]
    }
    # batch mode của SQLite tạo lại bảng → mọi ràng buộc cũ phải còn nguyên
    assert schema["checks"] == VIDEO_0006_CHECKS | {
        "ck_videos_attempt_count_non_negative",
        "ck_videos_lease_matches_status",
    }
    assert schema["uniques"] == {
        "uq_videos_owner_id_active_slot",
        "uq_videos_output_storage_key",
        "uq_videos_poster_storage_key",
    }
    assert schema["fks"] == {"fk_videos_owner_id_users", "fk_videos_input_image_id_media_assets"}
    assert schema["indexes"]["ix_videos_status_lease_expires_at"] == ["status", "lease_expires_at"]
    assert {"ix_videos_owner_id_created_at", "ix_videos_status_created_at"} <= set(
        schema["indexes"]
    )


def test_upgrade_0007_keeps_existing_queued_videos(tmp_path: Path) -> None:
    database_path = tmp_path / "migrate.db"
    config = alembic_config(database_path)
    command.upgrade(config, "0006")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO users (id, email, password_hash, full_name, role, is_active, "
                "email_verified, created_at, updated_at) VALUES (1, 'a@example.com', 'x', 'A', "
                "'user', 1, 0, '2026-11-15 00:00:00', '2026-11-15 00:00:00')"
            )
            connection.exec_driver_sql(
                "INSERT INTO videos (id, owner_id, prompt, aspect_ratio, duration_seconds, "
                "model_key, status, active_slot, progress, created_at) VALUES (1, 1, 'p', '16:9', "
                "5, 'mock', 'queued', 1, 0, '2026-11-15 00:00:00')"
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        with engine.connect() as connection:
            row = connection.exec_driver_sql(
                "SELECT status, active_slot, attempt_count, lease_token FROM videos WHERE id = 1"
            ).one()
    finally:
        engine.dispose()
    assert tuple(row) == ("queued", 1, 0, None)


def test_downgrade_to_0006_removes_only_lease_columns(tmp_path: Path) -> None:
    database_path = tmp_path / "migrate.db"
    config = alembic_config(database_path)
    command.upgrade(config, "head")

    command.downgrade(config, "0006")

    schema = videos_schema(database_path)
    assert not LEASE_COLUMNS & set(schema["columns"])
    assert schema["checks"] == VIDEO_0006_CHECKS
    assert "ix_videos_status_lease_expires_at" not in schema["indexes"]


def test_upgrade_after_downgrade_to_0006_restores_lease_columns(tmp_path: Path) -> None:
    database_path = tmp_path / "migrate.db"
    config = alembic_config(database_path)
    command.upgrade(config, "head")
    command.downgrade(config, "0006")

    command.upgrade(config, "head")

    assert set(videos_schema(database_path)["columns"]) >= LEASE_COLUMNS
