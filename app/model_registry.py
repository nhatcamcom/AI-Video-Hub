"""Nạp mọi ORM model để `Base.metadata` đầy đủ.

Dùng bởi Alembic (autogenerate / so sánh schema) và test. Thêm model của module mới vào đây.
"""

from app.modules.audit import models as audit_models
from app.modules.auth import models as auth_models
from app.modules.users import models as users_models

__all__ = ["audit_models", "auth_models", "users_models"]
