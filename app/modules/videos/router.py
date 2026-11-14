from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.core.errors import error_response_doc
from app.modules.auth.dependencies import CurrentUserDep
from app.modules.videos.catalog import VideoModelCatalog
from app.modules.videos.schemas import VideoModelListResponse, VideoModelResponse

router = APIRouter(prefix="/video-models", tags=["videos"])


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
