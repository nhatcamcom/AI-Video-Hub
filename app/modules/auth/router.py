import logging

from fastapi import APIRouter, BackgroundTasks, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.database import SessionFactoryDep
from app.core.dependencies import (
    EmailSenderDep,
    RateLimiterDep,
    RateLimitSubjectDep,
    SettingsDep,
    enforce_rate_limits,
)
from app.core.email import EmailMessage, EmailSender
from app.core.errors import OpenApiResponses, error_response_doc
from app.modules.auth.cookies import clear_session_cookies, set_device_cookie, set_session_cookies
from app.modules.auth.dependencies import AuthServiceDep, CurrentUserDep, OptionalAuthDep
from app.modules.auth.emails import build_password_reset_email
from app.modules.auth.rate_limits import (
    forgot_password_checks,
    login_checks,
    register_checks,
    reset_password_checks,
)
from app.modules.auth.schemas import (
    ForgotPasswordRequest,
    LoginRequest,
    MessageResponse,
    RegisterRequest,
    ResetPasswordRequest,
)
from app.modules.auth.service import AuthService
from app.modules.users.schemas import UserResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

FORGOT_RESPONSE_MESSAGE = (
    "Nếu email có trong hệ thống, chúng tôi đã gửi hướng dẫn đặt lại mật khẩu. "
    "Vui lòng kiểm tra hộp thư."
)
RESET_DONE_MESSAGE = "Mật khẩu đã được đặt lại. Vui lòng đăng nhập bằng mật khẩu mới."

RATE_LIMITED = error_response_doc(429, "Gửi quá nhiều yêu cầu")
VALIDATION_FAILED = error_response_doc(422, "Dữ liệu không hợp lệ")
CSRF_FAILED = error_response_doc(403, "Thiếu hoặc sai CSRF token")
REGISTER_RESPONSES: OpenApiResponses = {
    **error_response_doc(409, "Email đã được đăng ký"),
    **VALIDATION_FAILED,
    **RATE_LIMITED,
}
LOGIN_RESPONSES: OpenApiResponses = {
    **error_response_doc(401, "Email hoặc mật khẩu không đúng"),
    **VALIDATION_FAILED,
    **RATE_LIMITED,
}
RESET_RESPONSES: OpenApiResponses = {
    **error_response_doc(400, "Token không hợp lệ, hết hạn hoặc đã dùng"),
    **VALIDATION_FAILED,
    **RATE_LIMITED,
}


async def _send_email_safely(sender: EmailSender, message: EmailMessage) -> None:
    # Chạy nền sau khi đã trả response; lỗi gửi thư không được làm lộ việc email tồn tại
    try:
        await sender.send(message)
    except Exception:
        logger.exception("Failed to send email %r", message.subject)


async def _issue_password_reset(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    sender: EmailSender,
    email: str,
) -> None:
    """Chạy nền SAU khi đã trả 202: việc tra email / tạo token không ảnh hưởng thời gian phản hồi,
    nên không đo được email có tồn tại hay không. Lỗi chỉ ghi log (không ghi email, không token).
    """
    try:
        async with session_factory() as session:
            issued = await AuthService(session, settings).request_password_reset(email)
    except Exception:
        logger.exception("Failed to issue password reset token")
        return
    if issued is not None:
        message = build_password_reset_email(settings, issued.email, issued.token)
        await _send_email_safely(sender, message)


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Đăng ký tài khoản (luôn có vai trò USER)",
    responses=REGISTER_RESPONSES,
)
async def register(
    payload: RegisterRequest,
    service: AuthServiceDep,
    limiter: RateLimiterDep,
    client_subject: RateLimitSubjectDep,
) -> UserResponse:
    enforce_rate_limits(limiter, register_checks(client_subject))
    user = await service.register(payload)
    return UserResponse.model_validate(user)


@router.post(
    "/login",
    response_model=UserResponse,
    summary="Đăng nhập — đặt cookie phiên (HttpOnly) và cookie CSRF",
    responses=LOGIN_RESPONSES,
)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    service: AuthServiceDep,
    limiter: RateLimiterDep,
    client_subject: RateLimitSubjectDep,
    settings: SettingsDep,
) -> UserResponse:
    device = await service.find_trusted_device(
        request.cookies.get(settings.device_cookie_key), payload.email
    )
    if device is None:
        # Trình duyệt quen của chính tài khoản này không bị giới hạn IP/mạng (vd. CGNAT, /48 đang
        # bị tấn công); nó vẫn chịu ngân sách riêng theo thiết bị trong database
        enforce_rate_limits(limiter, login_checks(client_subject))
    user = await service.authenticate(payload.email, payload.password, device=device)
    issued = await service.start_session(
        user, replacing_token=request.cookies.get(settings.session_cookie_key)
    )
    device_token = await service.trust_device(user, device)
    set_session_cookies(response, settings, issued)
    if device_token is not None:
        set_device_cookie(response, settings, device_token)
    return UserResponse.model_validate(user)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Đăng xuất — thu hồi phiên phía server và xóa cookie",
    responses=CSRF_FAILED,
)
async def logout(
    context: OptionalAuthDep, service: AuthServiceDep, settings: SettingsDep
) -> Response:
    if context is not None:
        await service.revoke_session(context.session)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session_cookies(response, settings)
    return response


@router.get(
    "/me",
    response_model=UserResponse,
    summary="Tài khoản đang đăng nhập",
    responses=error_response_doc(401, "Chưa đăng nhập"),
)
async def read_current_user(user: CurrentUserDep) -> UserResponse:
    return UserResponse.model_validate(user)


@router.post(
    "/forgot-password",
    response_model=MessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Yêu cầu email đặt lại mật khẩu (phản hồi giống nhau dù email có tồn tại hay không)",
    responses={**VALIDATION_FAILED, **RATE_LIMITED},
)
async def forgot_password(
    payload: ForgotPasswordRequest,
    background_tasks: BackgroundTasks,
    service: AuthServiceDep,
    limiter: RateLimiterDep,
    client_subject: RateLimitSubjectDep,
    email_sender: EmailSenderDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> MessageResponse:
    enforce_rate_limits(limiter, forgot_password_checks(client_subject))
    await service.reserve_password_reset_request(payload.email)
    background_tasks.add_task(
        _issue_password_reset, session_factory, settings, email_sender, payload.email
    )
    return MessageResponse(message=FORGOT_RESPONSE_MESSAGE)


@router.post(
    "/reset-password",
    response_model=MessageResponse,
    summary="Đặt mật khẩu mới bằng token trong email (dùng một lần, có hạn)",
    responses=RESET_RESPONSES,
)
async def reset_password(
    payload: ResetPasswordRequest,
    service: AuthServiceDep,
    limiter: RateLimiterDep,
    client_subject: RateLimitSubjectDep,
) -> MessageResponse:
    enforce_rate_limits(limiter, reset_password_checks(client_subject))
    await service.reset_password(payload.token, payload.new_password)
    return MessageResponse(message=RESET_DONE_MESSAGE)
