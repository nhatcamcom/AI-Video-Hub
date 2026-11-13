from app.core.config import Settings
from app.core.email import EmailMessage


def build_password_reset_email(settings: Settings, recipient: str, token: str) -> EmailMessage:
    # Token đặt sau '#' (fragment): trình duyệt không gửi phần này lên server,
    # nên token không bao giờ xuất hiện trong access log của web server/proxy.
    reset_link = f"{settings.public_base_url.rstrip('/')}/reset-password#token={token}"
    body = (
        "Xin chào,\n\n"
        "Chúng tôi nhận được yêu cầu đặt lại mật khẩu cho tài khoản AI Video Hub của bạn.\n"
        f"Mở liên kết sau trong vòng {settings.password_reset_ttl_minutes} phút:\n\n"
        f"{reset_link}\n\n"
        "Liên kết chỉ dùng được một lần. Nếu bạn không yêu cầu, hãy bỏ qua email này — "
        "mật khẩu của bạn vẫn giữ nguyên.\n"
    )
    return EmailMessage(to=recipient, subject="Đặt lại mật khẩu AI Video Hub", body=body)
