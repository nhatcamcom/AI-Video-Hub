"""Worker tạo video: `python -m app.worker [--exit-when-idle]` (P2-7).

Tiến trình RIÊNG, khởi động / dừng độc lập với web server; chỉ trao đổi qua bảng `videos` và thư
mục storage. Entrypoint mỏng: dựng phụ thuộc, đăng ký tín hiệu dừng, chạy vòng lặp, dừng an toàn.
Phase 2 chỉ có MockProvider — bản demo, KHÔNG gọi nhà cung cấp AI thật.

Tín hiệu: SIGINT, SIGTERM và SIGBREAK (chỉ khi hệ điều hành có). Dùng `signal.signal` +
`call_soon_threadsafe` vì event loop trên Windows không hỗ trợ `add_signal_handler`. SIGKILL /
`TerminateProcess` không bắt được: lease hết hạn → reaper phục hồi job, orphan / hạn mức tính thừa
do P2-8 xử lý. Thời hạn dừng chỉ giới hạn các bước của worker; tổng thời gian tiến trình kết thúc có
thể dài hơn (asyncio.run còn hủy / chờ task và chờ thread của executor).
"""

import argparse
import asyncio
import logging
import signal
import sys
from collections.abc import Callable, Sequence
from types import FrameType

from app.core.background_tasks import BACKGROUND_TASKS, ShutdownResult, shutdown_background
from app.core.config import Settings, get_settings
from app.core.database import create_engine, create_session_factory
from app.core.storage import create_storage
from app.modules.videos.catalog import build_video_model_catalog
from app.modules.videos.jobs import VideoJobService
from app.modules.videos.output import VideoOutputStore
from app.modules.videos.providers.registry import build_provider_registry
from app.modules.videos.runner import VideoWorker, image_checker

logger = logging.getLogger("app.worker")

STOP_SIGNAL_NAMES = ("SIGINT", "SIGTERM", "SIGBREAK")
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def install_signal_handlers(
    loop: asyncio.AbstractEventLoop, on_signal: Callable[[], None]
) -> Callable[[], None]:
    """Đăng ký tín hiệu dừng (bỏ qua tín hiệu hệ điều hành không có); trả hàm khôi phục."""
    previous: dict[int, object] = {}

    def handle(_signum: int, _frame: FrameType | None) -> None:
        loop.call_soon_threadsafe(on_signal)

    for name in STOP_SIGNAL_NAMES:
        signum = getattr(signal, name, None)
        if signum is not None:
            previous[signum] = signal.signal(signum, handle)

    def restore() -> None:
        for signum, handler in previous.items():
            signal.signal(signum, handler)  # type: ignore[arg-type]

    return restore


async def run_worker(settings: Settings, *, exit_when_idle: bool = False) -> ShutdownResult:
    catalog = build_video_model_catalog(settings.enabled_video_models)  # cấu hình sai → dừng ngay
    storage = create_storage(settings)
    providers = build_provider_registry(catalog, settings, storage)
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    jobs = VideoJobService(session_factory, settings)
    worker = VideoWorker(
        settings=settings,
        jobs=jobs,
        output=VideoOutputStore(session_factory, storage, settings, jobs),
        catalog=catalog,
        providers=providers,
        image_available=image_checker(session_factory, storage, settings),
        exit_when_idle=exit_when_idle,
    )
    restore = install_signal_handlers(asyncio.get_running_loop(), worker.request_stop)
    logger.info("Worker bắt đầu (model: %s)", ", ".join(spec.key for spec in catalog.enabled()))
    try:
        await worker.run()
    finally:
        restore()
        result = await shutdown_background(
            BACKGROUND_TASKS,
            engine,
            drain_seconds=settings.shutdown_drain_seconds,
            cancel_grace_seconds=settings.shutdown_cancel_grace_seconds,
        )
    logger.info("Worker dừng (engine đã dispose: %s)", result.engine_disposed)
    return result


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.worker", description="Worker tạo video")
    parser.add_argument(
        "--exit-when-idle", action="store_true", help="Thoát khi hàng đợi rỗng (dùng khi kiểm thử)"
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    asyncio.run(run_worker(get_settings(), exit_when_idle=args.exit_when_idle))


if __name__ == "__main__":
    main()
