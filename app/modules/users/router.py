from typing import Annotated

from fastapi import APIRouter, Query

from app.core.database import DbDep
from app.core.errors import OpenApiResponses, error_response_doc
from app.modules.auth.dependencies import AdminUserDep, CurrentUserDep
from app.modules.users.schemas import UserListResponse, UserProfileUpdate, UserResponse
from app.modules.users.service import UserService

router = APIRouter(prefix="/users", tags=["users"])

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

LIST_USERS_RESPONSES: OpenApiResponses = {
    **error_response_doc(401, "Chưa đăng nhập"),
    **error_response_doc(403, "Không phải ADMIN"),
}
UPDATE_PROFILE_RESPONSES: OpenApiResponses = {
    **error_response_doc(401, "Chưa đăng nhập"),
    **error_response_doc(403, "Thiếu hoặc sai CSRF token"),
    **error_response_doc(422, "Dữ liệu không hợp lệ hoặc có field bị cấm"),
}


@router.get(
    "",
    response_model=UserListResponse,
    summary="Danh sách người dùng (chỉ ADMIN)",
    responses=LIST_USERS_RESPONSES,
)
async def list_users(
    _: AdminUserDep,
    db: DbDep,
    skip: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> UserListResponse:
    users, total = await UserService(db).list_users(skip=skip, limit=limit)
    return UserListResponse(total=total, items=[UserResponse.model_validate(u) for u in users])


@router.patch(
    "/me",
    response_model=UserResponse,
    summary="Cập nhật hồ sơ của chính mình (chỉ full_name; không đổi được email/role)",
    responses=UPDATE_PROFILE_RESPONSES,
)
async def update_my_profile(
    payload: UserProfileUpdate, user: CurrentUserDep, db: DbDep
) -> UserResponse:
    updated = await UserService(db).update_profile(user, payload)
    return UserResponse.model_validate(updated)
