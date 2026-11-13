"""Môi trường chạy migration Alembic (async, dùng chung engine factory với app)."""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection

import app.model_registry  # noqa: F401  — nạp mọi model vào Base.metadata
from app.core.config import get_settings
from app.core.database import Base, create_migration_engine

config = context.config
target_metadata = Base.metadata

# Test đặt configure_logger=False để không đè cấu hình logging của pytest
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def _database_url() -> str:
    # Ưu tiên URL truyền vào (test, công cụ); mặc định lấy từ Settings → không lặp secret trong .ini
    return config.get_main_option("sqlalchemy.url") or get_settings().database_url


def _configure(**options: object) -> None:
    context.configure(
        target_metadata=target_metadata,
        render_as_batch=True,  # SQLite cần batch mode để ALTER bảng
        compare_type=True,
        **options,  # type: ignore[arg-type]
    )


def run_migrations_offline() -> None:
    _configure(url=_database_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def _run_sync_migrations(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_migration_engine(_database_url())  # hide_parameters=True như engine của app
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_sync_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
