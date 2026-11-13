from fastapi import APIRouter

from app.core.database import DbDep
from app.core.dependencies import SettingsDep
from app.core.errors import ErrorResponse, ServiceUnavailableError
from app.modules.health.schemas import HealthResponse
from app.modules.health.service import is_database_available

router = APIRouter(prefix="/health", tags=["health"])


@router.get(
    "",
    response_model=HealthResponse,
    summary="Kiểm tra tình trạng dịch vụ",
    responses={503: {"model": ErrorResponse, "description": "Database không khả dụng"}},
)
async def read_health(db: DbDep, settings: SettingsDep) -> HealthResponse:
    if not await is_database_available(db):
        raise ServiceUnavailableError(
            "Cơ sở dữ liệu tạm thời không khả dụng", code="database_unavailable"
        )
    return HealthResponse(
        status="ok",
        app=settings.app_name,
        version=settings.app_version,
        environment=settings.app_env,
        database="ok",
    )
