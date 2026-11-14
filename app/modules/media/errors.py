from app.core.errors import AppError, ServiceUnavailableError

# Request chờ quá lâu / hàng chờ đầy: thử lại sau ngần này giây
PROCESSING_BUSY_RETRY_AFTER_SECONDS = 10


class UnsupportedImageTypeError(AppError):
    """415: không phải PNG/JPEG/WebP theo magic bytes (không xét content_type hay tên file)."""

    status_code = 415
    code = "unsupported_media_type"
    default_message = "Chỉ hỗ trợ ảnh PNG, JPEG hoặc WebP."


class AnimatedImageError(AppError):
    status_code = 415
    code = "animated_image_not_supported"
    default_message = "Không hỗ trợ ảnh động. Hãy dùng ảnh tĩnh."


class InvalidImageError(AppError):
    """422: đúng chữ ký định dạng nhưng không giải mã được (hỏng, bị cắt cụt, giả mạo)."""

    status_code = 422
    code = "invalid_image"
    default_message = "Không đọc được ảnh. File có thể bị hỏng."


class ImageDimensionsTooLargeError(AppError):
    status_code = 422
    code = "image_dimensions_too_large"
    default_message = "Ảnh có độ phân giải quá lớn."


class ImageProcessingBusyError(ServiceUnavailableError):
    code = "image_processing_busy"
    default_message = "Máy chủ đang xử lý nhiều ảnh. Vui lòng thử lại sau ít giây."

    def __init__(self) -> None:
        super().__init__(headers={"Retry-After": str(PROCESSING_BUSY_RETRY_AFTER_SECONDS)})
