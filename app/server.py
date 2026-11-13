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

APP_FACTORY = "app.main:create_app"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def build_server_options(argv: Sequence[str]) -> dict[str, Any]:
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
    }


def main(argv: Sequence[str] | None = None) -> None:
    uvicorn.run(**build_server_options(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    main()
