"""Task nền dùng chung trong MỘT tiến trình (P2-7, W8) và dừng an toàn (W10 web / W11 worker).

- `BackgroundTaskRegistry` giữ tham chiếu MẠNH tới task dọn dẹp / requeue (asyncio chỉ giữ tham
  chiếu yếu) và chỉ bỏ một task qua `done_callback`, tức là sau khi task đã kết thúc.
- `shutdown_background`: drain → hủy task quá hạn → drain thêm → chỉ `engine.dispose()` khi không
  còn task nào có thể dùng session / connection. Còn task sống → log critical, giữ tham chiếu,
  KHÔNG dispose. Không bao giờ trả quota hay xóa file chỉ để kịp dừng: task bị hủy tự đi theo
  nhánh "dọn dẹp bị gián đoạn" (giữ quota, log "cần đối soát").

Thời hạn ở đây chỉ giới hạn các bước của chúng ta. Sau khi hàm chính trả về, `asyncio.run` còn hủy
và chờ các task còn lại, rồi chờ thread của executor — tổng thời gian tiến trình kết thúc có thể
dài hơn.
"""

import asyncio
import logging
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class DisposableEngine(Protocol):
    async def dispose(self) -> None: ...


class BackgroundTaskRegistry:
    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    def track(self, task: asyncio.Task[Any]) -> asyncio.Task[Any]:
        self._tasks.add(task)
        task.add_done_callback(self._forget)
        return task

    def spawn(self, coroutine: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any]:
        return self.track(asyncio.create_task(coroutine, name=name))

    def pending(self) -> tuple[asyncio.Task[Any], ...]:
        return tuple(task for task in self._tasks if not task.done())

    def _forget(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            # Đọc exception để không mất lỗi; chỉ ghi tên task và loại lỗi (không dữ liệu nhạy cảm)
            logger.error(
                "Task nền %s kết thúc với lỗi %s",
                task.get_name(),
                type(task.exception()).__name__,
            )

    async def drain(self, within_seconds: float) -> tuple[asyncio.Task[Any], ...]:
        """Chờ tới khi hết task hoặc hết `within_seconds`; trả các task CÒN SỐNG (không hủy chúng).

        Mỗi vòng lấy lại `pending()`, nên task mới phát sinh trong lúc drain cũng được chờ.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + within_seconds
        while True:
            pending = self.pending()
            remaining = deadline - loop.time()
            if not pending or remaining <= 0:
                return pending
            await asyncio.wait(pending, timeout=remaining)


# Một registry cho mỗi tiến trình (web hoặc worker)
BACKGROUND_TASKS = BackgroundTaskRegistry()


@dataclass(frozen=True)
class ShutdownResult:
    engine_disposed: bool
    alive: int


async def shutdown_background(
    registry: BackgroundTaskRegistry,
    engine: DisposableEngine,
    *,
    drain_seconds: float,
    cancel_grace_seconds: float,
) -> ShutdownResult:
    alive = await registry.drain(drain_seconds)
    if alive:
        logger.warning("Còn %d task nền sau %.0f giây; hủy và chờ thêm", len(alive), drain_seconds)
        for task in alive:
            task.cancel()
        alive = await registry.drain(cancel_grace_seconds)
    if alive:
        logger.critical(
            "Còn %d task nền chưa kết thúc sau khi hủy (%s): KHÔNG dispose engine, giữ tham chiếu; "
            "file và hạn mức giữ nguyên, cần đối soát",
            len(alive),
            ", ".join(sorted(task.get_name() for task in alive)),
        )
        return ShutdownResult(engine_disposed=False, alive=len(alive))
    await engine.dispose()
    return ShutdownResult(engine_disposed=True, alive=0)
