"""Migration Alembic: chạy được lên/xuống và khớp đúng với ORM model."""

from pathlib import Path

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


def test_revision_0004_follows_0003_and_is_head() -> None:
    scripts = ScriptDirectory.from_config(Config(str(PROJECT_ROOT / "alembic.ini")))

    revision = scripts.get_revision("0004")

    assert revision is not None
    assert revision.down_revision == "0003"
    assert scripts.get_current_head() == "0004"


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
    assert before - after == {QUOTA_TABLE}
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
