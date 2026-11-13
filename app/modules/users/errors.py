from app.core.errors import ConflictError


class EmailAlreadyRegisteredError(ConflictError):
    code = "email_already_registered"
    default_message = "Email này đã được đăng ký"


class AccountInactiveError(ConflictError):
    code = "account_inactive"
    default_message = "Tài khoản đang bị khóa"
