from app.core.errors import AppError, ConflictError


class VideoModelUnavailableError(AppError):
    """422: model không có trong danh mục HOẶC đã tắt — cùng một thông báo cho cả hai."""

    status_code = 422
    code = "video_model_unavailable"
    default_message = "Model video không khả dụng."


class UnsupportedVideoOptionsError(AppError):
    status_code = 422
    code = "unsupported_video_options"
    default_message = "Model không hỗ trợ tỷ lệ khung hình hoặc thời lượng đã chọn."


class InputImageNotSupportedError(AppError):
    status_code = 422
    code = "input_image_not_supported"
    default_message = "Model này không nhận ảnh đầu vào."


class InputImageNotFoundError(AppError):
    """422: ảnh không tồn tại, không thuộc người dùng hoặc đã xóa — cùng một response, không lộ
    ảnh của người khác có tồn tại hay không."""

    status_code = 422
    code = "input_image_not_found"
    default_message = "Không tìm thấy ảnh đầu vào."


class ActiveVideoLimitError(ConflictError):
    code = "active_video_limit_reached"
    default_message = "Bạn đang có tối đa số video đang xử lý. Hãy chờ video hiện tại hoàn tất."


class VideoSlotContentionError(RuntimeError):
    """Lỗi lập trình / dữ liệu bất thường: tranh chấp slot lặp lại dù vẫn báo còn slot trống.
    Không phải lỗi nghiệp vụ → 500 (chi tiết chỉ nằm trong log server, không có prompt)."""
