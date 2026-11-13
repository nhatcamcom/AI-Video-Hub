from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.modules.audit.service import ADMIN_ROLE_GRANTED, AuditService
from app.modules.users.errors import AccountInactiveError, EmailAlreadyRegisteredError
from app.modules.users.models import User, UserRole
from app.modules.users.schemas import UserProfileUpdate, normalize_email


@dataclass(frozen=True)
class PromotionResult:
    user: User
    was_already_admin: bool


class UserService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_id(self, user_id: int) -> User | None:
        return await self.db.get(User, user_id)

    async def get_by_email(self, email: str) -> User | None:
        result = await self.db.execute(select(User).where(User.email == normalize_email(email)))
        return result.scalar_one_or_none()

    async def create(self, *, email: str, password_hash: str, full_name: str) -> User:
        """Tạo tài khoản USER. Không có tham số role: tài khoản mới không thể là ADMIN."""
        user = User(
            email=normalize_email(email),
            password_hash=password_hash,
            full_name=full_name,
            role=UserRole.USER,
        )
        self.db.add(user)
        try:
            # Dựa vào ràng buộc UNIQUE của DB thay vì kiểm tra trước (tránh race condition)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise EmailAlreadyRegisteredError from exc
        await self.db.refresh(user)
        return user

    async def update_profile(self, user: User, payload: UserProfileUpdate) -> User:
        user.full_name = payload.full_name
        await self.db.commit()
        await self.db.refresh(user)
        return user

    async def list_users(self, *, skip: int, limit: int) -> tuple[list[User], int]:
        total = (await self.db.execute(select(func.count(User.id)))).scalar_one()
        result = await self.db.execute(select(User).order_by(User.id).offset(skip).limit(limit))
        return list(result.scalars()), total

    async def promote_to_admin(self, email: str, *, actor: str) -> PromotionResult:
        """Chỉ gọi từ CLI quản trị (`python -m app.cli promote-admin`), không có API tương ứng.

        Đổi role và ghi audit trong CÙNG một transaction. Tài khoản bị khóa bị từ chối.
        """
        user = await self.get_by_email(email)
        if user is None:
            raise NotFoundError("Người dùng", normalize_email(email))
        if not user.is_active:
            raise AccountInactiveError
        if user.role is UserRole.ADMIN:
            return PromotionResult(user=user, was_already_admin=True)
        user.role = UserRole.ADMIN
        event = AuditService(self.db).record(
            actor=actor, action=ADMIN_ROLE_GRANTED, target_user_id=user.id
        )
        await self.db.commit()
        AuditService.log_committed(event)
        await self.db.refresh(user)
        return PromotionResult(user=user, was_already_admin=False)

    def set_password_hash(self, user: User, password_hash: str) -> None:
        """Đổi hash mật khẩu trong transaction hiện tại; người gọi chịu trách nhiệm commit."""
        user.password_hash = password_hash
