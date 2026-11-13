from app.core.errors import AppError, ForbiddenError, UnauthorizedError


class InvalidCredentialsError(UnauthorizedError):
    """Dùng chung cho mọi lý do đăng nhập thất bại — không tiết lộ email có tồn tại hay không."""

    code = "invalid_credentials"
    default_message = "Email hoặc mật khẩu không đúng"


class CsrfValidationError(ForbiddenError):
    code = "csrf_failed"
    default_message = "Yêu cầu không hợp lệ (thiếu hoặc sai CSRF token). Vui lòng tải lại trang."


class InvalidResetTokenError(AppError):
    """Dùng chung cho token không tồn tại / hết hạn / đã dùng."""

    status_code = 400
    code = "invalid_reset_token"
    default_message = "Liên kết đặt lại mật khẩu không hợp lệ hoặc đã hết hạn"
