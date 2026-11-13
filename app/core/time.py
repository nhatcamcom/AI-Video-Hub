from datetime import UTC, datetime


def utc_now() -> datetime:
    """Thời điểm hiện tại, luôn có múi giờ UTC (không bao giờ dùng datetime naive)."""
    return datetime.now(UTC)
