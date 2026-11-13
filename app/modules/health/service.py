import logging

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def is_database_available(session: AsyncSession) -> bool:
    """True nếu DB trả lời được truy vấn đơn giản nhất."""
    try:
        await session.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError):
        # OSError: driver mạng (asyncpg) có thể ném lỗi kết nối chưa được SQLAlchemy bọc lại
        logger.warning("Database health check failed", exc_info=True)
        return False
    return True
