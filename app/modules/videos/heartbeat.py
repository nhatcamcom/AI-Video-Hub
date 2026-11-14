"""Heartbeat gia hạn lease (P2-7, W4): task asyncio RIÊNG, độc lập với provider.

Mỗi `interval` giây gia hạn lease bằng CAS theo token. CAS trả 0 (worker khác đã lấy job / job đã
bị đổi) → báo mất lease một lần rồi dừng. Lỗi DB tạm thời → thử lại ở nhịp sau, nhưng nếu theo
đồng hồ lease đã hết hạn thì cũng coi là mất. Bị hủy → `CancelledError` được ném lại nguyên vẹn.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from app.core.time import utc_now
from app.modules.videos.jobs import VideoJobService
from app.modules.videos.providers.base import VideoJob

logger = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]


class LeaseHeartbeat:
    def __init__(
        self,
        jobs: VideoJobService,
        job: VideoJob,
        *,
        interval: float,
        lease_seconds: float,
        on_lost: Callable[[], None],
        clock: Callable[[], datetime] = utc_now,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.jobs = jobs
        self.job = job
        self.interval = interval
        self.lease = timedelta(seconds=lease_seconds)
        self.on_lost = on_lost
        self.clock = clock
        self.sleep = sleep

    async def run(self) -> None:
        lease_until = self.clock() + self.lease
        while True:
            await self.sleep(self.interval)
            try:
                renewed = await self.jobs.renew_lease(self.job)
            except Exception as exc:  # CancelledError không bị bắt (BaseException)
                logger.warning(
                    "Heartbeat video %s lỗi (%s); thử lại ở nhịp sau",
                    self.job.id,
                    type(exc).__name__,
                )
                if self.clock() >= lease_until:
                    self._lose()
                    return
                continue
            if not renewed:
                self._lose()
                return
            lease_until = self.clock() + self.lease

    def _lose(self) -> None:
        logger.warning("Video %s: mất lease, worker này dừng xử lý job", self.job.id)
        self.on_lost()
