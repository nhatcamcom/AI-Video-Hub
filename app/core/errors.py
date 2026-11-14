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
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

INTERNAL_ERROR_MESSAGE = "Đã có lỗi xảy ra, vui lòng thử lại sau"

PAYLOAD_TOO_LARGE_CODE = "payload_too_large"
PAYLOAD_TOO_LARGE_MESSAGE = "Dữ liệu gửi lên vượt quá dung lượng cho phép"
HTTP_ERROR_CODES: Mapping[int, tuple[str, str]] = {
    404: ("not_found", "Không tìm thấy tài nguyên"),
    405: ("method_not_allowed", "Phương thức không được hỗ trợ"),
    # 413 từ RequestBodyLimitMiddleware khi body vượt giới hạn trong lúc đang đọc
    413: (PAYLOAD_TOO_LARGE_CODE, PAYLOAD_TOO_LARGE_MESSAGE),
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


class PayloadTooLargeError(AppError):
    status_code = 413
    code = PAYLOAD_TOO_LARGE_CODE
    default_message = PAYLOAD_TOO_LARGE_MESSAGE


class PayloadTooLargeEnvelopeMiddleware:
    """Đổi phản hồi 413 KHÔNG phải JSON thành envelope chuẩn `payload_too_large`.

    `RequestBodyLimitMiddleware` của Starlette tự trả `413` dạng text thuần khi `Content-Length`
    vượt giới hạn (thay cả phản hồi của app). Middleware này phải đặt NGOÀI nó; mọi phản hồi khác
    — kể cả 413 đã là JSON — đi qua nguyên vẹn.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        is_replacing = False

        async def send_normalized(message: Message) -> None:
            nonlocal is_replacing
            if message["type"] == "http.response.start" and message["status"] == 413:
                content_type = Headers(raw=message.get("headers", [])).get("content-type", "")
                if not content_type.startswith("application/json"):
                    is_replacing = True
                    envelope = build_error_response(
                        413, PAYLOAD_TOO_LARGE_CODE, PAYLOAD_TOO_LARGE_MESSAGE
                    )
                    await send(
                        {
                            "type": "http.response.start",
                            "status": 413,
                            "headers": envelope.raw_headers,
                        }
                    )
                    await send({"type": "http.response.body", "body": envelope.body})
                    return
            if is_replacing:
                return  # bỏ phần thân text thuần của phản hồi gốc
            await send(message)

        await self.app(scope, receive, send_normalized)


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
