from app.core.errors import ConflictError


class StorageQuotaExceededError(ConflictError):
    """409: giữ chỗ sẽ làm dung lượng đã dùng vượt hạn mức (quyết định 7.6)."""

    code = "storage_quota_exceeded"
    default_message = "Đã hết dung lượng lưu trữ cho phép. Hãy xóa bớt file rồi thử lại."


class QuotaUserNotFoundError(LookupError):
    """Lỗi lập trình: giữ chỗ cho user_id không tồn tại (phía gọi phải truyền user đã xác thực)."""
