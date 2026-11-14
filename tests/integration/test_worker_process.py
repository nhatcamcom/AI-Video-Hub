"""Tiến trình worker THẬT (`python -m app.worker`): dừng có kiểm soát và chết đột ngột (P2-7).

Chạy trên DB SQLite + storage tạm; `cwd` là thư mục tạm nên `.env` của dự án không được đọc.
"""

import os
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy import insert

import app.model_registry  # noqa: F401  — nạp mọi model vào metadata
from app.core.database import Base
from app.modules.users.models import User
from app.modules.videos.models import Video

pytestmark = pytest.mark.integration

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAIN_DATABASE = (PROJECT_ROOT / "ai_video_hub.db").resolve()
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x05" * 4096
PROMPT = "prompt-tien-trinh-bi-mat"
WAIT_SECONDS = 60
POLL_SECONDS = 0.1
LEASE_SECONDS = 6


def prepare(tmp_path: Path) -> Path:
    database = (tmp_path / "worker.db").resolve()
    assert database != MAIN_DATABASE and not database.is_relative_to(PROJECT_ROOT)
    engine = create_sync_engine(f"sqlite:///{database.as_posix()}")
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(
                insert(User).values(id=1, email="p@example.com", password_hash="x", full_name="P")
            )
            connection.execute(
                insert(Video).values(
                    id=1,
                    owner_id=1,
                    prompt=PROMPT,
                    aspect_ratio="16:9",
                    duration_seconds=5,
                    model_key="mock",
                    status="queued",
                    active_slot=1,
                    progress=0,
                )
            )
    finally:
        engine.dispose()
    (tmp_path / "sample.mp4").write_bytes(MP4)
    return database


def worker_env(tmp_path: Path, database: Path, processing_seconds: float) -> dict[str, str]:
    env = {
        key: value for key, value in os.environ.items() if not key.startswith(("APP_", "DATABASE"))
    }
    env |= {
        "PYTHONPATH": str(PROJECT_ROOT),
        "PYTHONIOENCODING": "utf-8",  # console Windows mặc định không phải UTF-8 (log tiếng Việt)
        "DATABASE_URL": f"sqlite+aiosqlite:///{database.as_posix()}",
        "STORAGE_ROOT": str(tmp_path / "storage"),
        "APP_ENV": "test",
        "EMAIL_BACKEND": "memory",
        "MOCK_SAMPLE_VIDEO_PATH": str(tmp_path / "sample.mp4"),
        "MOCK_PROCESSING_SECONDS": str(processing_seconds),
        "WORKER_LEASE_SECONDS": str(LEASE_SECONDS),
        "WORKER_HEARTBEAT_SECONDS": "1",
        "WORKER_POLL_SECONDS": "0.2",
        "WORKER_SHUTDOWN_GRACE_SECONDS": "0.5",
        "SHUTDOWN_DRAIN_SECONDS": "5",
        "SHUTDOWN_CANCEL_GRACE_SECONDS": "2",
    }
    return env


def start_worker(tmp_path: Path, env: dict[str, str], *args: str) -> subprocess.Popen[bytes]:
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    return subprocess.Popen(  # noqa: S603 — lệnh cố định: python của venv + module của dự án
        [sys.executable, "-m", "app.worker", *args],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=flags,
    )


def video(database: Path) -> tuple[str, int, str | None, str | None]:
    connection = sqlite3.connect(database)
    try:
        status, attempts, token, expires = connection.execute(
            "SELECT status, attempt_count, lease_token, lease_expires_at FROM videos WHERE id = 1"
        ).fetchone()
    finally:
        connection.close()
    return status, attempts, token, expires


def wait_until(predicate: object, timeout: float = WAIT_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return
        time.sleep(POLL_SECONDS)
    raise AssertionError("hết thời gian chờ")


@pytest.fixture
def processes() -> Iterator[list[subprocess.Popen[bytes]]]:
    started: list[subprocess.Popen[bytes]] = []
    yield started
    for process in started:  # không để lại tiến trình nào sau test
        if process.poll() is None:
            process.kill()
            process.wait(WAIT_SECONDS)


def output_of(process: subprocess.Popen[bytes]) -> str:
    assert process.stdout is not None
    return process.stdout.read().decode("utf-8", errors="replace")


def graceful_stop(process: subprocess.Popen[bytes]) -> None:
    if sys.platform == "win32":
        os.kill(process.pid, signal.CTRL_BREAK_EVENT)  # → SIGBREAK trong tiến trình con
    else:
        process.send_signal(signal.SIGTERM)


def test_signal_stops_worker_and_requeues_job_without_counting_the_attempt(
    tmp_path: Path, processes: list[subprocess.Popen[bytes]]
) -> None:
    database = prepare(tmp_path)
    process = start_worker(tmp_path, worker_env(tmp_path, database, processing_seconds=30))
    processes.append(process)

    wait_until(lambda: video(database)[0] == "processing")
    graceful_stop(process)
    code = process.wait(WAIT_SECONDS)
    logs = output_of(process)

    status, attempts, token, _ = video(database)
    assert code == 0, logs
    assert (status, attempts, token) == ("queued", 0, None)
    assert "engine đã dispose: True" in logs
    assert PROMPT not in logs


def test_killed_worker_job_is_recovered_by_the_next_worker(
    tmp_path: Path, processes: list[subprocess.Popen[bytes]]
) -> None:
    database = prepare(tmp_path)
    first = start_worker(tmp_path, worker_env(tmp_path, database, processing_seconds=30))
    processes.append(first)
    wait_until(lambda: video(database)[0] == "processing")

    first.kill()  # SIGKILL / TerminateProcess: không có cơ hội dọn dẹp — như worker chết
    first.wait(WAIT_SECONDS)
    assert video(database)[0] == "processing"  # job kẹt cho tới khi lease hết hạn
    time.sleep(LEASE_SECONDS + 1)  # chờ lease hết hạn thật (không có đồng hồ tiêm ở tiến trình con)

    second = start_worker(
        tmp_path, worker_env(tmp_path, database, processing_seconds=0), "--exit-when-idle"
    )
    processes.append(second)
    code = second.wait(WAIT_SECONDS)
    logs = output_of(second)

    status, attempts, token, _ = video(database)
    assert code == 0, logs
    assert (status, attempts, token) == ("succeeded", 2, None)  # lượt bị giết vẫn được tính
    stored = list((tmp_path / "storage" / "videos").iterdir())
    assert len(stored) == 1 and stored[0].read_bytes() == MP4
    assert PROMPT not in logs
