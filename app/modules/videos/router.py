from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request, Response

from app.core.database import DbDep
from app.core.dependencies import (
    RateLimiterDep,
    RateLimitSubjectDep,
    SettingsDep,
    enforce_rate_limits,
)
from app.core.errors import OpenApiResponses, error_response_doc
from app.modules.auth.dependencies import CurrentUserDep
from app.modules.media.router import MediaServiceDep
from app.modules.videos.catalog import VideoModelCatalog
from app.modules.videos.rate_limits import video_create_checks
from app.modules.videos.schemas import (
    VideoCreate,
    VideoModelListResponse,
    VideoModelResponse,
    VideoResponse,
)
from app.modules.videos.service import VideoService

router = APIRouter(prefix="/video-models", tags=["videos"])
videos_router = APIRouter(prefix="/videos", tags=["videos"])

CREATE_RESPONSES: OpenApiResponses = {
    **error_response_doc(401, "Chưa đăng nhập"),
    **error_response_doc(403, "Thiếu hoặc sai CSRF token"),
    **error_response_doc(
        409, "Đã có 2 video đang xử lý (active_video_limit_reached) hoặc hết dung lượng"
    ),
    **error_response_doc(
        422,
        "Dữ liệu không hợp lệ, model không khả dụng, tùy chọn không được hỗ trợ "
        "hoặc ảnh đầu vào không hợp lệ",
    ),
    **error_response_doc(429, "Vượt giới hạn tần suất tạo video"),
}
READ_RESPONSES: OpenApiResponses = {
    **error_response_doc(401, "Chưa đăng nhập"),
    **error_response_doc(404, "Không có video này hoặc video không thuộc về bạn"),
}
VideoIdPath = Annotated[int, Path(ge=1)]


def get_video_model_catalog(request: Request) -> VideoModelCatalog:
    catalog: VideoModelCatalog = request.app.state.video_model_catalog
    return catalog


VideoModelCatalogDep = Annotated[VideoModelCatalog, Depends(get_video_model_catalog)]


@router.get(
    "",
    response_model=VideoModelListResponse,
    summary="Danh sách model video đang bật (Phase 2: chỉ Mock, bản demo)",
    responses=error_response_doc(401, "Chưa đăng nhập"),
)
async def list_video_models(
    _: CurrentUserDep, catalog: VideoModelCatalogDep
) -> VideoModelListResponse:
    items = [VideoModelResponse.model_validate(spec) for spec in catalog.enabled()]
    return VideoModelListResponse(total=len(items), items=items)


def get_video_service(
    db: DbDep, settings: SettingsDep, catalog: VideoModelCatalogDep, media: MediaServiceDep
) -> VideoService:
    return VideoService(db, settings, catalog, media)


VideoServiceDep = Annotated[VideoService, Depends(get_video_service)]


@videos_router.post(
    "",
    response_model=VideoResponse,
    status_code=202,
    summary="Tạo yêu cầu video (xếp hàng cho worker; Phase 2 chỉ có Mock, bản demo)",
    responses=CREATE_RESPONSES,
)
async def create_video(
    payload: VideoCreate,
    user: CurrentUserDep,
    service: VideoServiceDep,
    limiter: RateLimiterDep,
    subject: RateLimitSubjectDep,
    request: Request,
    response: Response,
) -> VideoResponse:
    # CurrentUserDep (session + CSRF) đã chạy; tiếp theo rate limit IP rồi rate limit user
    enforce_rate_limits(limiter, video_create_checks(subject))
    await service.reserve_create_rate_limit(user.id)
    video = await service.create_video(user.id, payload)
    response.headers["Location"] = request.url_for("get_video", video_id=video.id).path
    return VideoResponse.model_validate(video)


@videos_router.get(
    "/{video_id}",
    response_model=VideoResponse,
    summary="Trạng thái và thông tin video của chính mình",
    responses=READ_RESPONSES,
)
async def get_video(
    video_id: VideoIdPath, user: CurrentUserDep, service: VideoServiceDep
) -> VideoResponse:
    return VideoResponse.model_validate(await service.get_video(user.id, video_id))
