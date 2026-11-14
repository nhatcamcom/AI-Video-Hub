from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse
from starlette.datastructures import UploadFile

from app.core.database import DbDep
from app.core.dependencies import (
    RateLimiterDep,
    RateLimitSubjectDep,
    SettingsDep,
    StorageDep,
    enforce_rate_limits,
)
from app.core.errors import OpenApiResponses, error_response_doc
from app.modules.auth.dependencies import CurrentUserDep
from app.modules.media.image_processing import ImageProcessingGate
from app.modules.media.rate_limits import media_upload_checks
from app.modules.media.schemas import MediaImageResponse
from app.modules.media.service import MediaService

router = APIRouter(prefix="/media/images", tags=["media"])

# Ảnh thuộc về một user: không cho proxy / trình duyệt lưu cache dùng chung, không cho trang
# khác origin nhúng (kèm `nosniff` + CSP chung từ SecurityHeadersMiddleware)
IMAGE_CONTENT_HEADERS = {
    "Cache-Control": "private, no-store",
    "Cross-Origin-Resource-Policy": "same-origin",
}

UPLOAD_RESPONSES: OpenApiResponses = {
    **error_response_doc(401, "Chưa đăng nhập"),
    **error_response_doc(403, "Thiếu hoặc sai CSRF token"),
    **error_response_doc(409, "Hết dung lượng lưu trữ (storage_quota_exceeded)"),
    **error_response_doc(413, "File vượt dung lượng cho phép (payload_too_large)"),
    **error_response_doc(
        415, "Không phải PNG/JPEG/WebP hoặc là ảnh động (unsupported_media_type, ...)"
    ),
    **error_response_doc(422, "Ảnh hỏng (invalid_image) hoặc quá nhiều pixel"),
    **error_response_doc(429, "Vượt giới hạn tần suất upload"),
    **error_response_doc(503, "Máy chủ đang bận xử lý ảnh (image_processing_busy)"),
}
READ_RESPONSES: OpenApiResponses = {
    **error_response_doc(401, "Chưa đăng nhập"),
    **error_response_doc(404, "Không có ảnh này hoặc ảnh không thuộc về bạn"),
}

ImageIdPath = Annotated[int, Path(ge=1)]

# Body upload: đúng MỘT file ở field `file`, không field văn bản nào (thừa → 400 của Starlette)
UPLOAD_FILE_FIELD = "file"
MAX_UPLOAD_FILES = 1
MAX_UPLOAD_TEXT_FIELDS = 0
# Endpoint không khai báo tham số File (xem upload_image) nên phải tự mô tả body cho OpenAPI —
# giữ đúng schema FastAPI sinh ra trước đây
UPLOAD_OPENAPI_EXTRA: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": [UPLOAD_FILE_FIELD],
                    "properties": {
                        UPLOAD_FILE_FIELD: {
                            "type": "string",
                            "contentMediaType": "application/octet-stream",
                            "title": "File",
                            "description": "Ảnh PNG, JPEG hoặc WebP",
                        }
                    },
                }
            }
        },
    }
}


def _missing_file_error() -> RequestValidationError:
    """Cùng lỗi FastAPI trả khi thiếu tham số File bắt buộc → envelope 422 không đổi."""
    return RequestValidationError(
        [
            {
                "type": "missing",
                "loc": ("body", UPLOAD_FILE_FIELD),
                "msg": "Field required",
                "input": None,
            }
        ]
    )


def get_image_gate(request: Request) -> ImageProcessingGate:
    gate: ImageProcessingGate = request.app.state.image_gate
    return gate


def get_media_service(
    db: DbDep,
    storage: StorageDep,
    gate: Annotated[ImageProcessingGate, Depends(get_image_gate)],
    settings: SettingsDep,
) -> MediaService:
    return MediaService(db, storage, gate, settings)


MediaServiceDep = Annotated[MediaService, Depends(get_media_service)]


@router.post(
    "",
    response_model=MediaImageResponse,
    status_code=201,
    summary="Upload ảnh đầu vào (PNG/JPEG/WebP; metadata bị loại bỏ)",
    responses=UPLOAD_RESPONSES,
    openapi_extra=UPLOAD_OPENAPI_EXTRA,
)
async def upload_image(
    request: Request,
    user: CurrentUserDep,
    service: MediaServiceDep,
    limiter: RateLimiterDep,
    subject: RateLimitSubjectDep,
    response: Response,
) -> MediaImageResponse:
    # Không khai báo tham số File: khi đó FastAPI parse TOÀN BỘ multipart trước mọi dependency.
    # Ở đây session + CSRF (CurrentUserDep), rate limit IP rồi rate limit user đều chạy trước, nên
    # request bị từ chối không làm server đọc hay ghi tạm byte body nào.
    enforce_rate_limits(limiter, media_upload_checks(subject))
    await service.reserve_upload_rate_limit(user.id)
    async with request.form(
        max_files=MAX_UPLOAD_FILES, max_fields=MAX_UPLOAD_TEXT_FIELDS
    ) as form:  # thoát khối → đóng file tạm của Starlette
        upload = form.get(UPLOAD_FILE_FIELD)
        if not isinstance(upload, UploadFile):
            raise _missing_file_error()
        asset = await service.upload_image(user.id, upload)
    response.headers["Location"] = request.url_for("get_image", image_id=asset.id).path
    return MediaImageResponse.model_validate(asset)


@router.get(
    "/{image_id}",
    response_model=MediaImageResponse,
    summary="Metadata ảnh của chính mình",
    responses=READ_RESPONSES,
)
async def get_image(
    image_id: ImageIdPath, user: CurrentUserDep, service: MediaServiceDep
) -> MediaImageResponse:
    return MediaImageResponse.model_validate(await service.get_image(user.id, image_id))


@router.get(
    "/{image_id}/content",
    response_class=FileResponse,
    summary="Nội dung ảnh của chính mình (đã làm sạch metadata)",
    responses=READ_RESPONSES,
)
async def get_image_content(
    image_id: ImageIdPath, user: CurrentUserDep, service: MediaServiceDep
) -> FileResponse:
    asset = await service.get_image(user.id, image_id)
    path = await service.image_content_path(asset)
    return FileResponse(path, media_type=asset.content_type, headers=IMAGE_CONTENT_HEADERS)
