from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy.pool import NullPool

from app.core.database import UTCDateTime, create_engine, create_migration_engine
from app.core.time import utc_now
from tests.conftest import build_settings

pytestmark = pytest.mark.unit

UTC_PLUS_7 = timezone(timedelta(hours=7))


def test_app_engine_hides_sql_parameters_from_errors_and_logs() -> None:
    engine = create_engine(build_settings())

    assert engine.sync_engine.hide_parameters is True


def test_migration_engine_hides_sql_parameters_and_uses_no_pool() -> None:
    engine = create_migration_engine("sqlite+aiosqlite:///:memory:")

    assert engine.sync_engine.hide_parameters is True
    assert isinstance(engine.sync_engine.pool, NullPool)


def test_utc_now_is_timezone_aware_utc() -> None:
    now = utc_now()

    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_utc_datetime_converts_aware_values_to_utc_on_write() -> None:
    local = datetime(2026, 11, 13, 21, 0, tzinfo=UTC_PLUS_7)

    stored = UTCDateTime().process_bind_param(local, dialect=None)  # type: ignore[arg-type]

    assert stored == datetime(2026, 11, 13, 14, 0, tzinfo=UTC)


def test_utc_datetime_rejects_naive_values() -> None:
    with pytest.raises(ValueError, match="timezone"):
        UTCDateTime().process_bind_param(datetime(2026, 1, 1), dialect=None)  # type: ignore[arg-type]


def test_utc_datetime_marks_naive_db_values_as_utc_on_read() -> None:
    # SQLite không lưu múi giờ → giá trị đọc ra là naive nhưng thực chất là UTC
    loaded = UTCDateTime().process_result_value(datetime(2026, 1, 1, 8, 0), dialect=None)  # type: ignore[arg-type]

    assert loaded == datetime(2026, 1, 1, 8, 0, tzinfo=UTC)


def test_utc_datetime_normalizes_aware_db_values_to_utc_on_read() -> None:
    # PostgreSQL (timestamptz) trả về datetime có múi giờ
    loaded = UTCDateTime().process_result_value(
        datetime(2026, 1, 1, 15, 0, tzinfo=UTC_PLUS_7),
        dialect=None,  # type: ignore[arg-type]
    )

    assert loaded == datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
    assert loaded is not None
    assert loaded.utcoffset() == timedelta(0)


def test_utc_datetime_passes_none_through() -> None:
    column_type = UTCDateTime()

    assert column_type.process_bind_param(None, dialect=None) is None  # type: ignore[arg-type]
    assert column_type.process_result_value(None, dialect=None) is None  # type: ignore[arg-type]
