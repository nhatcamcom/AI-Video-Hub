"""Các trang HTML tĩnh và file tĩnh tại `/static` (không render phía server).

Mỗi trang có một đường dẫn cố định trong `PAGE_FILES` (danh sách trắng): đường dẫn từ request
không bao giờ được ghép thành đường dẫn file.
"""

from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# Thư mục static/ ở gốc dự án, tính theo vị trí file này chứ không theo thư mục đang chạy lệnh
STATIC_DIR = Path(__file__).resolve().parents[2] / "static"
STATIC_URL_PATH = "/static"
HTML_MEDIA_TYPE = "text/html"

PAGE_FILES: Mapping[str, str] = {
    "/": "index.html",
    "/login": "login.html",
    "/register": "register.html",
    "/forgot-password": "forgot-password.html",
    # Link trong email đặt lại mật khẩu (app/modules/auth/emails.py) trỏ cố định tới đường dẫn này
    "/reset-password": "reset-password.html",
}

router = APIRouter(include_in_schema=False)


def _page_endpoint(page_file: Path) -> Callable[[], Awaitable[FileResponse]]:
    async def read_page() -> FileResponse:
        return FileResponse(page_file, media_type=HTML_MEDIA_TYPE)

    return read_page


for page_path, file_name in PAGE_FILES.items():
    router.add_api_route(
        page_path,
        _page_endpoint(STATIC_DIR / file_name),
        methods=["GET"],
        name=f"page:{file_name}",
    )


def mount_static_files(app: FastAPI) -> None:
    # StaticFiles tự chặn path traversal (đường dẫn sau khi chuẩn hóa phải nằm trong STATIC_DIR),
    # không liệt kê thư mục (html=False) và không theo symlink ra ngoài (follow_symlink=False)
    app.mount(STATIC_URL_PATH, StaticFiles(directory=STATIC_DIR), name="static")
