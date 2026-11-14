"""Khởi động server: `python -m app.server [--host HOST] [--port PORT] [--reload]`.

Luôn tắt `proxy_headers` của uvicorn. Mặc định uvicorn tin X-Forwarded-For từ 127.0.0.1/::1,
nên bất kỳ tiến trình nào trên máy (hoặc proxy cấu hình sai) có thể giả mạo IP client và né rate
limit. Việc tin header proxy do app tự xử lý, chỉ với các IP trong TRUSTED_PROXY_IPS.
"""

import argparse
import sys
from collections.abc import Sequence
from typing import Any

import uvicorn

from app.core.config import get_settings

APP_FACTORY = "app.main:create_app"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
# Quá hạn này uvicorn hủy request còn treo, rồi lifespan chờ task dọn dẹp nền (P2-7, W10)
DEFAULT_GRACEFUL_SHUTDOWN_SECONDS = 20


def build_server_options(
    argv: Sequence[str], graceful_shutdown_seconds: int = DEFAULT_GRACEFUL_SHUTDOWN_SECONDS
) -> dict[str, Any]:
    parser = argparse.ArgumentParser(prog="python -m app.server", description="Chạy AI Video Hub")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--reload", action="store_true", help="Tự nạp lại khi sửa code (chỉ dev)")
    args = parser.parse_args(argv)
    return {
        "app": APP_FACTORY,
        "factory": True,
        "host": args.host,
        "port": args.port,
        "reload": args.reload,
        "proxy_headers": False,
        "timeout_graceful_shutdown": graceful_shutdown_seconds,
    }


def main(argv: Sequence[str] | None = None) -> None:
    options = build_server_options(
        sys.argv[1:] if argv is None else argv,
        graceful_shutdown_seconds=get_settings().server_graceful_shutdown_seconds,
    )
    uvicorn.run(**options)


if __name__ == "__main__":
    main()
