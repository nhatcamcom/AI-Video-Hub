import pytest

from app.core import errors

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (errors.NotFoundError("Video", 42), 404, "not_found"),
        (errors.ValidationError("Tiêu đề không hợp lệ"), 422, "validation_error"),
        (errors.UnauthorizedError(), 401, "unauthorized"),
        (errors.ForbiddenError(), 403, "forbidden"),
        (errors.ConflictError("Email đã được sử dụng"), 409, "conflict"),
        (errors.RateLimitError(retry_after_seconds=30), 429, "rate_limit_exceeded"),
        (errors.ServiceUnavailableError(), 503, "service_unavailable"),
    ],
    ids=["not-found", "validation", "unauthorized", "forbidden", "conflict", "rate-limit", "503"],
)
def test_app_errors_carry_status_and_code(
    error: errors.AppError, status_code: int, code: str
) -> None:
    assert isinstance(error, errors.AppError)
    assert error.status_code == status_code
    assert error.code == code
    assert str(error)  # luôn có thông điệp cho người dùng


def test_not_found_error_names_the_resource() -> None:
    assert "Video" in str(errors.NotFoundError("Video", 42))


def test_rate_limit_error_sets_retry_after_header() -> None:
    assert errors.RateLimitError(retry_after_seconds=30).headers == {"Retry-After": "30"}


def test_app_error_allows_overriding_code_and_message() -> None:
    error = errors.ServiceUnavailableError("CSDL không khả dụng", code="database_unavailable")

    assert error.code == "database_unavailable"
    assert str(error) == "CSDL không khả dụng"
    assert error.status_code == 503
