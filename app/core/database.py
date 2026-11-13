"""Kết nối database async (SQLite khi dev → PostgreSQL khi mở rộng, chỉ đổi DATABASE_URL)."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends, Request
from sqlalchemy import BigInteger, DateTime, Dialect, Integer, MetaData, event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool
from sqlalchemy.types import TypeDecorator

from app.core.config import Settings

# Tên constraint cố định → migration Alembic ổn định và giống nhau giữa SQLite / PostgreSQL
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# BIGINT trên PostgreSQL; SQLite chỉ tự tăng với INTEGER PRIMARY KEY
IdType = BigInteger().with_variant(Integer(), "sqlite")


class Base(DeclarativeBase):
    """Base cho mọi ORM model của các module."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UTCDateTime(TypeDecorator[datetime]):
    """Thời gian luôn lưu và đọc ra ở UTC có múi giờ.

    SQLite không lưu múi giờ nên giá trị đọc ra là naive; kiểu này gắn lại UTC để mọi so sánh
    thời gian (hết hạn session, token…) đều giữa hai datetime aware.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("UTCDateTime chỉ nhận datetime có timezone")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def _enable_sqlite_foreign_keys(dbapi_connection: Any, _: Any) -> None:
    # SQLite mặc định TẮT khóa ngoại → bật cho từng kết nối để DB tự bảo vệ toàn vẹn dữ liệu
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def create_engine(settings: Settings) -> AsyncEngine:
    engine = create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        # Lỗi DB không được mang theo tham số SQL (email, hash mật khẩu, hash token) vào log
        hide_parameters=True,
    )
    if engine.dialect.name == "sqlite":
        event.listen(engine.sync_engine, "connect", _enable_sqlite_foreign_keys)
    return engine


def create_migration_engine(database_url: str) -> AsyncEngine:
    """Engine cho Alembic: không pool, che tham số SQL.

    Cố ý KHÔNG bật PRAGMA foreign_keys: batch mode của SQLite tạo lại bảng (copy → DROP → rename);
    nếu khóa ngoại đang bật, DROP bảng cha sẽ kích hoạt ON DELETE CASCADE và xóa dữ liệu bảng con.
    """
    return create_async_engine(database_url, poolclass=NullPool, hide_parameters=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """Một session cho mỗi request; rollback nếu handler ném lỗi."""
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


DbDep = Annotated[AsyncSession, Depends(get_db)]


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    """Cho tác vụ nền cần session RIÊNG (session của request đã đóng khi tác vụ chạy)."""
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return session_factory


SessionFactoryDep = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]
