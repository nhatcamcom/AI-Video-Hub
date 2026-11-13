"""Migration Alembic: chạy được lên/xuống và khớp đúng với ORM model."""

from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect

import app.model_registry  # noqa: F401  — nạp mọi model vào metadata
from app.core.database import Base

pytestmark = pytest.mark.integration

PROJECT_ROOT = Path(__file__).resolve().parents[2]
AUTH_TABLES = {"users", "user_sessions", "password_reset_tokens"}


def alembic_config(database_path: Path) -> Config:
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


def test_downgrade_removes_auth_tables(tmp_path: Path) -> None:
    database_path = tmp_path / "migrate.db"
    config = alembic_config(database_path)
    command.upgrade(config, "head")

    command.downgrade(config, "base")

    assert AUTH_TABLES.isdisjoint(table_names(database_path))
