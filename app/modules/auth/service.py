"""Nghiệp vụ xác thực: đăng ký, đăng nhập, phiên làm việc, đặt lại mật khẩu."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.config import Settings
from app.core.errors import RateLimitError
from app.core.time import utc_now
from app.modules.auth.errors import InvalidCredentialsError, InvalidResetTokenError
from app.modules.auth.models import PasswordResetToken, TrustedDevice, UserSession
from app.modules.auth.rate_limits import (
    ACCOUNT_LOGIN_RULES,
    DEVICE_LOGIN_RULES,
    FORGOT_PASSWORD_RULES,
)
from app.modules.auth.schemas import RegisterRequest
from app.modules.auth.security import (
    generate_token,
    hash_password_async,
    hash_token,
    password_needs_rehash,
    verify_password_against_dummy_async,
    verify_password_async,
)
from app.modules.auth.throttle import AccountThrottle
from app.modules.users.models import User
from app.modules.users.service import UserService


@dataclass(frozen=True)
class IssuedSession:
    """Token gốc chỉ tồn tại trong bộ nhớ để đặt vào cookie; DB chỉ giữ hash."""

    token: str
    csrf_token: str
    expires_at: datetime


@dataclass(frozen=True)
class IssuedResetToken:
    email: str
    token: str


@dataclass(frozen=True)
class PurgeResult:
    sessions_deleted: int
    reset_tokens_deleted: int
    trusted_devices_deleted: int = 0
    throttles_deleted: int = 0


# Giữ bản ghi đã hết hạn / thu hồi thêm một thời gian để điều tra sự cố, rồi mới xóa hẳn
AUTH_RECORD_RETENTION = timedelta(days=7)
# Mỗi tài khoản giữ tối đa ngần này thiết bị quen (mới nhất); cũ hơn bị xóa khi thêm thiết bị mới
MAX_TRUSTED_DEVICES_PER_USER = 10


class AuthService:
    def __init__(self, db: AsyncSession, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.users = UserService(db)
        self.throttle = AccountThrottle(db, settings.throttle_key)

    # ------------------------------------------------------------------ đăng ký / đăng nhập
    async def register(self, payload: RegisterRequest) -> User:
        password_hash = await hash_password_async(payload.password)
        return await self.users.create(
            email=payload.email, password_hash=password_hash, full_name=payload.full_name
        )

    async def authenticate(
        self, email: str, password: str, *, device: TrustedDevice | None = None
    ) -> User:
        """Đăng nhập. Mỗi lượt được giữ chỗ trong database TRƯỚC khi băm mật khẩu.

        Thiết bị quen (`device`) dùng ngân sách riêng của nó; mọi trường hợp khác dùng ngân sách
        theo email — kể cả email không tồn tại, để phản hồi giống hệt nhau.
        """
        rules, subject = (
            (DEVICE_LOGIN_RULES, str(device.id)) if device else (ACCOUNT_LOGIN_RULES, email)
        )
        retry_after = await self.throttle.reserve(rules, subject, utc_now())
        if retry_after is not None:
            raise RateLimitError(retry_after_seconds=retry_after)

        user = await self.users.get_by_email(email)
        if user is None:
            await verify_password_against_dummy_async(password)
            raise InvalidCredentialsError
        is_valid = await verify_password_async(user.password_hash, password)
        if not is_valid or not user.is_active:
            raise InvalidCredentialsError
        await self.throttle.clear(rules, subject)  # thành công → không còn là chuỗi sai liên tiếp
        if password_needs_rehash(user.password_hash):
            new_hash = await hash_password_async(password)
            self.users.set_password_hash(user, new_hash)  # commit cùng lúc tạo session
        return user

    # ------------------------------------------------------------------ thiết bị quen
    async def find_trusted_device(self, token: str | None, email: str) -> TrustedDevice | None:
        """Thiết bị còn hạn VÀ thuộc đúng tài khoản có email này; ngược lại None."""
        if not token:
            return None
        device = (
            await self.db.execute(
                select(TrustedDevice)
                .options(joinedload(TrustedDevice.user))
                .where(
                    TrustedDevice.token_hash == hash_token(token),
                    TrustedDevice.expires_at > utc_now(),
                )
            )
        ).scalar_one_or_none()
        if device is None or device.user.email != email or not device.user.is_active:
            return None
        return device

    async def trust_device(self, user: User, device: TrustedDevice | None) -> str | None:
        """Ghi nhận trình duyệt vừa đăng nhập thành công. Trả token mới để đặt cookie, hoặc None."""
        if device is not None:
            return None
        now = utc_now()
        token = generate_token()
        self.db.add(
            TrustedDevice(
                user_id=user.id,
                token_hash=hash_token(token),
                created_at=now,
                expires_at=now + timedelta(seconds=self.settings.trusted_device_ttl_seconds),
            )
        )
        await self.db.flush()
        oldest_beyond_limit = (
            select(TrustedDevice.id)
            .where(TrustedDevice.user_id == user.id)
            .order_by(TrustedDevice.created_at.desc(), TrustedDevice.id.desc())
            .offset(MAX_TRUSTED_DEVICES_PER_USER)
        )
        await self.db.execute(
            delete(TrustedDevice).where(TrustedDevice.id.in_(oldest_beyond_limit))
        )
        await self.db.commit()
        return token

    # ------------------------------------------------------------------ phiên làm việc
    async def start_session(self, user: User, *, replacing_token: str | None) -> IssuedSession:
        now = utc_now()
        if replacing_token:
            # Chống session fixation: phiên cũ trên trình duyệt này bị thu hồi khi đăng nhập lại
            await self.db.execute(
                update(UserSession)
                .where(
                    UserSession.token_hash == hash_token(replacing_token),
                    UserSession.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
        token, csrf_token = generate_token(), generate_token()
        expires_at = now + timedelta(seconds=self.settings.session_ttl_seconds)
        self.db.add(
            UserSession(
                user_id=user.id,
                token_hash=hash_token(token),
                csrf_token_hash=hash_token(csrf_token),
                created_at=now,
                expires_at=expires_at,
            )
        )
        await self.db.commit()
        return IssuedSession(token=token, csrf_token=csrf_token, expires_at=expires_at)

    async def resolve_session(self, token: str) -> UserSession | None:
        """Phiên còn hiệu lực (chưa thu hồi, chưa hết hạn, tài khoản còn hoạt động) hoặc None."""
        result = await self.db.execute(
            select(UserSession)
            .options(joinedload(UserSession.user))
            .where(
                UserSession.token_hash == hash_token(token),
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > utc_now(),
            )
        )
        session = result.scalar_one_or_none()
        if session is None or not session.user.is_active:
            return None
        return session

    async def revoke_session(self, session: UserSession) -> None:
        session.revoked_at = utc_now()
        await self.db.commit()

    async def _revoke_all_sessions(self, user_id: int, now: datetime) -> None:
        await self.db.execute(
            update(UserSession)
            .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
            .values(revoked_at=now)
        )

    async def purge_expired_records(self) -> PurgeResult:
        """Xóa session và token reset đã hết hạn / thu hồi quá AUTH_RECORD_RETENTION.

        Chỉ xóa bản ghi đã vô hiệu từ lâu, nên chạy định kỳ (Task Scheduler / cron) an toàn.
        """
        cutoff = utc_now() - AUTH_RECORD_RETENTION
        sessions = cast(
            CursorResult[Any],
            await self.db.execute(
                delete(UserSession).where(
                    or_(UserSession.expires_at < cutoff, UserSession.revoked_at < cutoff)
                )
            ),
        )
        tokens = cast(
            CursorResult[Any],
            await self.db.execute(
                delete(PasswordResetToken).where(PasswordResetToken.expires_at < cutoff)
            ),
        )
        devices = cast(
            CursorResult[Any],
            await self.db.execute(
                delete(TrustedDevice).where(TrustedDevice.expires_at < utc_now())
            ),
        )
        throttles = await self.throttle.purge_recovered(utc_now())
        await self.db.commit()
        return PurgeResult(
            sessions_deleted=sessions.rowcount,
            reset_tokens_deleted=tokens.rowcount,
            trusted_devices_deleted=devices.rowcount,
            throttles_deleted=throttles,
        )

    # ------------------------------------------------------------------ đặt lại mật khẩu
    async def reserve_password_reset_request(self, email: str) -> None:
        """Giới hạn số email reset tới MỘT địa chỉ — như nhau dù email có tồn tại hay không.

        Raises:
            RateLimitError: địa chỉ này đã nhận đủ số yêu cầu cho phép.
        """
        retry_after = await self.throttle.reserve(FORGOT_PASSWORD_RULES, email, utc_now())
        if retry_after is not None:
            raise RateLimitError(retry_after_seconds=retry_after)

    async def request_password_reset(self, email: str) -> IssuedResetToken | None:
        """Trả token nếu email tồn tại và còn hoạt động, ngược lại None.

        Router trả lời giống nhau trong cả hai trường hợp.
        """
        user = await self.users.get_by_email(email)
        if user is None or not user.is_active:
            return None
        now = utc_now()
        # Chỉ token mới nhất có hiệu lực: vô hiệu hóa mọi token cũ chưa dùng
        await self.db.execute(
            update(PasswordResetToken)
            .where(PasswordResetToken.user_id == user.id, PasswordResetToken.used_at.is_(None))
            .values(used_at=now)
        )
        token = generate_token()
        self.db.add(
            PasswordResetToken(
                user_id=user.id,
                token_hash=hash_token(token),
                created_at=now,
                expires_at=now + timedelta(minutes=self.settings.password_reset_ttl_minutes),
            )
        )
        await self.db.commit()
        return IssuedResetToken(email=user.email, token=token)

    async def reset_password(self, token: str, new_password: str) -> None:
        now = utc_now()
        stored = (
            await self.db.execute(
                select(PasswordResetToken).where(PasswordResetToken.token_hash == hash_token(token))
            )
        ).scalar_one_or_none()
        if stored is None or stored.used_at is not None or stored.expires_at <= now:
            raise InvalidResetTokenError
        # Băm trước khi ghi DB để không giữ khóa ghi trong lúc tính Argon2
        new_hash = await hash_password_async(new_password)

        # Đánh dấu đã dùng một cách nguyên tử: hai request song song chỉ một cái thắng
        claimed = cast(
            CursorResult[Any],
            await self.db.execute(
                update(PasswordResetToken)
                .where(PasswordResetToken.id == stored.id, PasswordResetToken.used_at.is_(None))
                .values(used_at=now)
            ),
        )
        user = await self.users.get_by_id(stored.user_id)
        if claimed.rowcount != 1 or user is None or not user.is_active:
            await self.db.rollback()
            raise InvalidResetTokenError
        self.users.set_password_hash(user, new_hash)
        await self._revoke_all_sessions(user.id, now)  # đăng xuất mọi thiết bị
        # Mật khẩu cũ có thể đã lộ: thiết bị quen cũ mất ngân sách riêng. Chủ tài khoản vừa chứng
        # minh quyền sở hữu email → mở lại bộ đếm đăng nhập để vào được ngay dù đang bị tấn công
        await self.db.execute(delete(TrustedDevice).where(TrustedDevice.user_id == user.id))
        await self.throttle.clear(ACCOUNT_LOGIN_RULES, user.email)
        await self.db.commit()
