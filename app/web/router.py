"""Trang khung tại `/` và file tĩnh tại `/static` (HTML tĩnh, không render phía server)."""

from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# Thư mục static/ ở gốc dự án, tính theo vị trí file này chứ không theo thư mục đang chạy lệnh
STATIC_DIR = Path(__file__).resolve().parents[2] / "static"
INDEX_PAGE = STATIC_DIR / "index.html"
STATIC_URL_PATH = "/static"
HTML_MEDIA_TYPE = "text/html"

router = APIRouter(include_in_schema=False)


@router.get("/")
async def read_index_page() -> FileResponse:
    return FileResponse(INDEX_PAGE, media_type=HTML_MEDIA_TYPE)


def mount_static_files(app: FastAPI) -> None:
    # StaticFiles tự chặn path traversal (đường dẫn sau khi chuẩn hóa phải nằm trong STATIC_DIR),
    # không liệt kê thư mục (html=False) và không theo symlink ra ngoài (follow_symlink=False)
    app.mount(STATIC_URL_PATH, StaticFiles(directory=STATIC_DIR), name="static")
