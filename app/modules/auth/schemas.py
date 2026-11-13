from typing import Annotated

from pydantic import AfterValidator, BaseModel, StringConstraints

from app.modules.users.schemas import FullName, NormalizedEmail, StrictRequest

# Theo NIST SP 800-63B: ưu tiên độ dài, không ép quy tắc ký tự; giới hạn trên chống DoS khi băm
PASSWORD_MIN_LENGTH = 10
PASSWORD_MAX_LENGTH = 128
RESET_TOKEN_MIN_LENGTH = 20
RESET_TOKEN_MAX_LENGTH = 128


def _reject_blank_password(password: str) -> str:
    if not password.strip():
        raise ValueError("Mật khẩu không được chỉ gồm khoảng trắng")
    return password


# Không strip mật khẩu: khoảng trắng là ký tự hợp lệ của mật khẩu
NewPassword = Annotated[
    str,
    StringConstraints(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH),
    AfterValidator(_reject_blank_password),
]
LoginPassword = Annotated[str, StringConstraints(min_length=1, max_length=PASSWORD_MAX_LENGTH)]


class RegisterRequest(StrictRequest):
    email: NormalizedEmail
    password: NewPassword
    full_name: FullName


class LoginRequest(StrictRequest):
    email: NormalizedEmail
    password: LoginPassword


class ForgotPasswordRequest(StrictRequest):
    email: NormalizedEmail


class ResetPasswordRequest(StrictRequest):
    token: Annotated[
        str, StringConstraints(min_length=RESET_TOKEN_MIN_LENGTH, max_length=RESET_TOKEN_MAX_LENGTH)
    ]
    new_password: NewPassword


class MessageResponse(BaseModel):
    message: str
