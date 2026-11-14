"""Cấu hình worker, mock provider và dừng an toàn (P2-7: W4, W7, W10, W11)."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.conftest import build_production_settings, build_settings

pytestmark = pytest.mark.unit

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MEBIBYTE = 1024 * 1024


def test_defaults_match_approved_decisions() -> None:
    settings = build_settings()

    assert settings.worker_lease_seconds == 60  # W4
    assert settings.worker_heartbeat_seconds == 10
    assert settings.worker_poll_seconds == 2
    assert settings.worker_max_attempts == 3
    assert settings.worker_retry_backoff_seconds == 15
    assert settings.worker_shutdown_grace_seconds == 30  # W11
    assert settings.shutdown_drain_seconds == 10  # W10 / W11
    assert settings.shutdown_cancel_grace_seconds == 5
    assert settings.server_graceful_shutdown_seconds == 20
    assert settings.mock_sample_video_path is None  # W7
    assert settings.mock_sample_video_file is None
    assert settings.mock_processing_seconds == 5
    assert settings.mock_force_failure is False
    assert settings.video_output_max_bytes == 100 * MEBIBYTE


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("storage/mock/sample.mp4", PROJECT_ROOT / "storage/mock/sample.mp4"), ("", None)],
    ids=["relative-to-project", "empty-means-unset"],
)
def test_mock_sample_path_is_resolved_from_project_root(raw: str, expected: Path | None) -> None:
    settings = build_settings(mock_sample_video_path=raw)

    resolved = settings.mock_sample_video_file
    assert resolved == (expected.resolve() if expected is not None else None)


def test_absolute_mock_sample_path_is_kept(tmp_path: Path) -> None:
    sample = tmp_path / "sample.mp4"

    assert build_settings(mock_sample_video_path=str(sample)).mock_sample_video_file == sample


@pytest.mark.parametrize(("heartbeat", "lease"), [(30, 60), (31, 60), (10, 15)])
def test_heartbeat_must_be_shorter_than_half_the_lease(heartbeat: float, lease: int) -> None:
    with pytest.raises(ValidationError, match="worker_heartbeat_seconds"):
        build_settings(worker_heartbeat_seconds=heartbeat, worker_lease_seconds=lease)


def test_heartbeat_just_under_half_the_lease_is_accepted() -> None:
    settings = build_settings(worker_heartbeat_seconds=29.9, worker_lease_seconds=60)

    assert settings.worker_heartbeat_seconds == 29.9


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("worker_lease_seconds", 0),
        ("worker_heartbeat_seconds", 0),
        ("worker_poll_seconds", 0),
        ("worker_max_attempts", 0),
        ("worker_retry_backoff_seconds", -1),
        ("worker_shutdown_grace_seconds", -1),
        ("shutdown_drain_seconds", -1),
        ("shutdown_cancel_grace_seconds", -1),
        ("server_graceful_shutdown_seconds", 0),
        ("mock_processing_seconds", -1),
        ("video_output_max_bytes", 0),
    ],
)
def test_out_of_range_values_are_rejected(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        build_settings(**{field: value})


def test_forced_mock_failure_is_refused_in_production() -> None:
    with pytest.raises(ValidationError, match="mock_force_failure"):
        build_production_settings(mock_force_failure=True)


def test_forced_mock_failure_is_allowed_outside_production() -> None:
    assert build_settings(mock_force_failure=True).mock_force_failure is True
