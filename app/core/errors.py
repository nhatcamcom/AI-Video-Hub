"""Hệ thống lỗi tập trung.

Mọi lỗi trả về client đều theo một envelope: {"error": {"code", "message", "details"?}}.
Chi tiết kỹ thuật chỉ được ghi log ở server, không bao giờ trả ra ngoài.
"""

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

INTERNAL_ERROR_MESSAGE = "Đã có lỗi xảy ra, vui lòng thử lại sau"

HTTP_ERROR_CODES: Mapping[int, tuple[str, str]] = {
    404: ("not_found", "Không tìm thấy tài nguyên"),
    405: ("method_not_allowed", "Phương thức không được hỗ trợ"),
}


# --------------------------------------------------------------------------- envelope (OpenAPI)
class ErrorDetail(BaseModel):
    field: str
    message: str
    code: str | None = None


class ErrorBody(BaseModel):
    code: str
    message: str
    details: list[ErrorDetail] | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


OpenApiResponses = dict[int | str, dict[str, Any]]


def error_response_doc(status_code: int, description: str) -> OpenApiResponses:
    """Mô tả một mã lỗi trong OpenAPI, dùng chung envelope ErrorResponse."""
    return {status_code: {"model": ErrorResponse, "description": description}}


# --------------------------------------------------------------------------- lỗi ứng dụng
class AppError(Exception):
    """Lỗi nghiệp vụ có chủ đích; subclass khai báo status_code, code, message mặc định."""

    status_code: int = 500
    code: str = "internal_error"
    default_message: str = INTERNAL_ERROR_MESSAGE

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message or self.default_message)
        if code is not None:
            self.code = code
        self.headers: dict[str, str] = dict(headers or {})


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"

    def __init__(self, resource: str, resource_id: str | int) -> None:
        super().__init__(f"{resource} không tồn tại: {resource_id}")


class ValidationError(AppError):
    status_code = 422
    code = "validation_error"
    default_message = "Dữ liệu không hợp lệ"


class UnauthorizedError(AppError):
    status_code = 401
    code = "unauthorized"
    default_message = "Vui lòng đăng nhập để tiếp tục"


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"
    default_message = "Bạn không có quyền thực hiện thao tác này"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"
    default_message = "Dữ liệu bị trùng hoặc xung đột"


class RateLimitError(AppError):
    status_code = 429
    code = "rate_limit_exceeded"
    default_message = "Thao tác quá nhanh, vui lòng thử lại sau"

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(headers={"Retry-After": str(retry_after_seconds)})


class ServiceUnavailableError(AppError):
    status_code = 503
    code = "service_unavailable"
    default_message = "Dịch vụ tạm thời không khả dụng"


# --------------------------------------------------------------------------- handler
def build_error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    details: Sequence[ErrorDetail] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorBody(code=code, message=message, details=list(details) if details else None)
    )
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(exclude_none=True),
        headers=dict(headers or {}),
    )


def _to_error_details(errors: Sequence[Any]) -> list[ErrorDetail]:
    return [
        ErrorDetail(
            field=".".join(str(part) for part in error["loc"]),
            message=str(error["msg"]),
            code=str(error["type"]),
        )
        for error in errors
    ]


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        if exc.status_code >= 500:
            logger.warning("AppError %s: %s", exc.code, exc)
        return build_error_response(exc.status_code, exc.code, str(exc), headers=exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code, message = HTTP_ERROR_CODES.get(exc.status_code, ("http_error", str(exc.detail)))
        return build_error_response(exc.status_code, code, message, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return build_error_response(
            422,
            "validation_error",
            "Dữ liệu không hợp lệ",
            details=_to_error_details(exc.errors()),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unexpected error", exc_info=exc)  # chi tiết chỉ nằm trong log server
        return build_error_response(500, "internal_error", INTERNAL_ERROR_MESSAGE)
