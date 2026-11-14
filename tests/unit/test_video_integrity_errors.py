"""Phân loại IntegrityError khi tạo video (P2-6): CHỈ tranh chấp slot mới được thử lại.

SQLite: dùng lỗi THẬT (bảng videos thật trên SQLite trong RAM). PostgreSQL: dùng lớp lỗi asyncpg
thật của SQLAlchemy bọc một lỗi driver giả có `sqlstate` / `constraint_name` (chưa có server
PostgreSQL và chưa cài asyncpg — rủi ro đã được chấp nhận).
"""

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, event, insert
from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_dbapi
from sqlalchemy.exc import IntegrityError

import app.model_registry  # noqa: F401  — nạp mọi model vào metadata
from app.core.database import Base
from app.modules.media.models import MediaAsset
from app.modules.users.models import User
from app.modules.videos.models import ACTIVE_SLOT_CONSTRAINT_NAME, Video
from app.modules.videos.service import (
    SQLITE_ACTIVE_SLOT_MESSAGE,
    IntegrityErrorKind,
    classify_integrity_error,
)

pytestmark = pytest.mark.unit

VIDEO_KEY = "videos/" + "e" * 32 + ".mp4"


@pytest.fixture
def engine() -> Iterator[Engine]:
    sqlite_engine = create_engine("sqlite://")
    event.listen(
        sqlite_engine, "connect", lambda conn, _: conn.cursor().execute("PRAGMA foreign_keys=ON")
    )
    Base.metadata.create_all(sqlite_engine)
    with sqlite_engine.begin() as connection:
        connection.execute(
            insert(User).values(id=1, email="a@example.com", password_hash="x", full_name="A")
        )
        connection.execute(
            insert(Video).values(id=1, owner_id=1, active_slot=1, output_storage_key=None, **BASE)
        )
        connection.execute(
            insert(Video).values(
                id=2,
                owner_id=1,
                active_slot=None,
                output_storage_key=VIDEO_KEY,
                output_size_bytes=5,
                **{**BASE, "status": "succeeded"},
            )
        )
    yield sqlite_engine
    sqlite_engine.dispose()


BASE: dict[str, Any] = {
    "prompt": "p",
    "aspect_ratio": "16:9",
    "duration_seconds": 5,
    "model_key": "mock",
    "status": "queued",
    "progress": 0,
}


def sqlite_error(engine: Engine, **values: Any) -> IntegrityError:
    with pytest.raises(IntegrityError) as caught, engine.begin() as connection:
        connection.execute(insert(Video).values(**{**BASE, **values}))
    return caught.value


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ({"owner_id": 1, "active_slot": 1}, IntegrityErrorKind.ACTIVE_SLOT_CONFLICT),
        (
            {
                "owner_id": 1,
                "active_slot": None,
                "status": "succeeded",
                "output_storage_key": VIDEO_KEY,
                "output_size_bytes": 5,
            },
            IntegrityErrorKind.OTHER,  # UNIQUE khác (output_storage_key) KHÔNG phải tranh chấp slot
        ),
        ({"owner_id": 1, "active_slot": 2, "input_image_id": 999}, IntegrityErrorKind.FOREIGN_KEY),
        ({"owner_id": 999, "active_slot": 1}, IntegrityErrorKind.FOREIGN_KEY),
        ({"owner_id": 1, "active_slot": 3}, IntegrityErrorKind.OTHER),  # CHECK
        ({"owner_id": 1, "active_slot": None}, IntegrityErrorKind.OTHER),  # CHECK slot/status
    ],
    ids=["slot", "other-unique", "fk-image", "fk-owner", "check-slot", "check-status"],
)
def test_real_sqlite_errors_are_classified(
    engine: Engine, values: dict[str, Any], expected: IntegrityErrorKind
) -> None:
    assert classify_integrity_error(sqlite_error(engine, **values)) is expected


def test_sqlite_message_used_by_classifier_matches_installed_sqlite(engine: Engine) -> None:
    # Nếu một phiên bản SQLite đổi định dạng thông điệp, test này fail rõ ràng thay vì retry sai
    error = sqlite_error(engine, owner_id=1, active_slot=1)

    assert str(error.orig) == SQLITE_ACTIVE_SLOT_MESSAGE
    assert getattr(error.orig, "sqlite_errorname", None) == "SQLITE_CONSTRAINT_UNIQUE"


class FakeAsyncpgError(Exception):
    def __init__(self, sqlstate: str | None, constraint_name: str | None) -> None:
        super().__init__("driver error")
        self.sqlstate = sqlstate
        self.constraint_name = constraint_name


def postgres_error(sqlstate: str | None, constraint_name: str | None) -> IntegrityError:
    # Lớp lỗi THẬT của SQLAlchemy (không có type hint) bọc lỗi driver giả
    driver_error = AsyncAdapt_asyncpg_dbapi.IntegrityError(  # type: ignore[no-untyped-call]
        "driver error", FakeAsyncpgError(sqlstate, constraint_name)
    )
    return IntegrityError("INSERT INTO videos ...", None, driver_error)


@pytest.mark.parametrize(
    ("sqlstate", "constraint_name", "expected"),
    [
        ("23505", ACTIVE_SLOT_CONSTRAINT_NAME, IntegrityErrorKind.ACTIVE_SLOT_CONFLICT),
        ("23505", "uq_videos_output_storage_key", IntegrityErrorKind.OTHER),
        ("23505", None, IntegrityErrorKind.OTHER),
        ("23503", "fk_videos_input_image_id_media_assets", IntegrityErrorKind.FOREIGN_KEY),
        ("23503", "fk_videos_owner_id_users", IntegrityErrorKind.FOREIGN_KEY),
        ("23514", "ck_videos_active_slot_allowed", IntegrityErrorKind.OTHER),
        ("23502", None, IntegrityErrorKind.OTHER),  # NOT NULL
        (None, ACTIVE_SLOT_CONSTRAINT_NAME, IntegrityErrorKind.OTHER),
    ],
    ids=[
        "slot",
        "other-unique",
        "unique-no-name",
        "fk-image",
        "fk-owner",
        "check",
        "not-null",
        "no-sqlstate",
    ],
)
def test_postgres_errors_are_classified(
    sqlstate: str | None, constraint_name: str | None, expected: IntegrityErrorKind
) -> None:
    assert classify_integrity_error(postgres_error(sqlstate, constraint_name)) is expected


def test_unrecognised_driver_error_is_other() -> None:
    error = IntegrityError("INSERT", None, Exception("UNIQUE constraint failed: videos.owner_id"))

    assert classify_integrity_error(error) is IntegrityErrorKind.OTHER


def test_media_model_is_loaded_for_foreign_keys() -> None:
    assert MediaAsset.__tablename__ in Base.metadata.tables
