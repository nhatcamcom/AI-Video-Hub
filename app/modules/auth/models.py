from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, IdType, UTCDateTime
from app.core.time import utc_now
from app.modules.users.models import User

TOKEN_HASH_LENGTH = 64  # SHA-256 dạng hex


class UserSession(Base):
    """Phiên đăng nhập phía server. Cookie chỉ chứa token ngẫu nhiên; DB chỉ lưu hash của nó."""

    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(IdType, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        IdType, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(TOKEN_HASH_LENGTH), unique=True)
    csrf_token_hash: Mapped[str] = mapped_column(String(TOKEN_HASH_LENGTH))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)

    # lazy="raise": buộc nạp user tường minh (joinedload) — tránh lazy load ngầm trong async
    user: Mapped[User] = relationship(lazy="raise")


class PasswordResetToken(Base):
    """Token đặt lại mật khẩu: chỉ lưu hash, có hạn dùng, dùng đúng một lần."""

    __tablename__ = "password_reset_tokens"

    id: Mapped[int] = mapped_column(IdType, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        IdType, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(TOKEN_HASH_LENGTH), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)


class TrustedDevice(Base):
    """Trình duyệt đã đăng nhập thành công: có ngân sách đăng nhập riêng (chống khóa tài khoản).

    Cookie chỉ chứa token ngẫu nhiên; DB chỉ lưu hash. Bị xóa khi đặt lại mật khẩu.
    """

    __tablename__ = "trusted_devices"

    id: Mapped[int] = mapped_column(IdType, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        IdType, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(TOKEN_HASH_LENGTH), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)

    user: Mapped[User] = relationship(lazy="raise")


class AuthThrottle(Base):
    """Bộ đếm brute-force theo tài khoản / thiết bị (thuật toán GCRA), dùng chung mọi worker.

    Khóa là HMAC của (rule, email hoặc id thiết bị) → không lưu email thô, kể cả email không tồn
    tại. `allowed_at_ms` là "thời điểm đến lý thuyết": hàng có giá trị đã qua là hàng thừa.
    """

    __tablename__ = "auth_throttles"

    key_hash: Mapped[str] = mapped_column(String(TOKEN_HASH_LENGTH), primary_key=True)
    allowed_at_ms: Mapped[int] = mapped_column(BigInteger, index=True)
