import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from tests.conftest import build_production_settings, build_settings, build_staging_settings

pytestmark = pytest.mark.unit


def test_settings_require_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(ValidationError, match="database_url"):
        Settings(_env_file=None)


def test_settings_read_values_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///./from-env.db")
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("ALLOWED_ORIGINS", '["https://staging.example.com"]')
    monkeypatch.setenv("COOKIE_SECURE", "true")  # bắt buộc ở staging
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://staging.example.com")  # bắt buộc https
    monkeypatch.setenv("EMAIL_BACKEND", "disabled")  # staging không ghi token reset ra đĩa
    monkeypatch.setenv("AUTH_THROTTLE_KEY", "env-throttle-key-0123456789abcdefghij")  # bắt buộc

    settings = Settings(_env_file=None)

    assert settings.database_url == "sqlite+aiosqlite:///./from-env.db"
    assert settings.app_env == "staging"
    assert settings.allowed_origins == ["https://staging.example.com"]


def test_get_settings_reads_environment_and_caches_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///./cached.db")
    get_settings.cache_clear()
    try:
        first = get_settings()
        second = get_settings()
    finally:
        get_settings.cache_clear()  # không để cache rò sang test khác

    assert first is second
    assert first.database_url == "sqlite+aiosqlite:///./cached.db"


def test_settings_default_to_safe_development_values() -> None:
    settings = build_settings(app_env="development")

    assert settings.debug is False
    assert "*" not in settings.allowed_origins
    assert settings.is_production is False


@pytest.mark.parametrize("app_env", ["development", "test"])
def test_is_production_false_outside_production(app_env: str) -> None:
    assert build_settings(app_env=app_env).is_production is False


def test_staging_is_not_production_but_is_hardened() -> None:
    settings = build_staging_settings()

    assert settings.is_production is False
    assert settings.is_hardened is True


@pytest.mark.parametrize(
    ("builder", "overrides", "field"),
    [
        ("staging", {"cookie_secure": False}, "cookie_secure"),
        ("staging", {"debug": True}, "debug"),
        ("staging", {"public_base_url": "http://staging.example.com"}, "https"),
        ("production", {"public_base_url": "http://app.example.com"}, "https"),
    ],
    ids=["staging-cookie", "staging-debug", "staging-http", "production-http"],
)
def test_hardened_environments_reject_unsafe_settings(
    builder: str, overrides: dict[str, str | bool], field: str
) -> None:
    build = build_staging_settings if builder == "staging" else build_production_settings
    with pytest.raises(ValidationError, match=field):
        build(**overrides)


@pytest.mark.parametrize("url", ["ftp://example.com", "javascript:alert(1)", "example.com"])
def test_public_base_url_must_be_http_or_https(url: str) -> None:
    with pytest.raises(ValidationError, match="public_base_url"):
        build_settings(public_base_url=url)


def test_session_cookie_uses_host_prefix_only_when_secure() -> None:
    assert build_settings().session_cookie_key == "avh_session"
    assert build_production_settings().session_cookie_key == "__Host-avh_session"
    # Cookie CSRF giữ tên cố định để JS của site luôn đọc được ở mọi môi trường
    assert build_production_settings().csrf_cookie_name == "avh_csrf"


def test_is_production_true_for_valid_production_config() -> None:
    assert build_production_settings().is_production is True


def test_production_requires_secure_cookies() -> None:
    with pytest.raises(ValidationError, match="cookie_secure"):
        build_production_settings(cookie_secure=False)


@pytest.mark.parametrize("backend", ["file", "memory"])
def test_production_rejects_development_email_backends(backend: str) -> None:
    # Hộp thư dev ghi nội dung email (kể cả token reset) ra đĩa/bộ nhớ — cấm trên production
    with pytest.raises(ValidationError, match="email_backend"):
        build_production_settings(email_backend=backend)


def test_trusted_proxies_default_to_none() -> None:
    assert build_settings().trusted_proxy_networks == ()


def test_trusted_proxies_are_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///./x.db")
    monkeypatch.setenv("TRUSTED_PROXY_IPS", '["10.0.0.5", "10.0.1.0/24"]')

    settings = Settings(_env_file=None)

    assert [str(network) for network in settings.trusted_proxy_networks] == [
        "10.0.0.5/32",
        "10.0.1.0/24",
    ]


@pytest.mark.parametrize("entry", ["*", "0.0.0.0/0", "::/0", "not-an-ip"])
def test_settings_reject_wildcard_or_invalid_trusted_proxies(entry: str) -> None:
    with pytest.raises(ValidationError, match="trusted_proxy_ips"):
        build_settings(trusted_proxy_ips=[entry])


def test_auth_defaults_are_safe() -> None:
    settings = build_settings()

    assert settings.cookie_secure is False  # dev chạy http; production bị ép True
    assert settings.session_ttl_hours > 0
    assert 5 <= settings.password_reset_ttl_minutes <= 60
    assert settings.rate_limit_enabled is True


def test_settings_reject_unknown_environment() -> None:
    with pytest.raises(ValidationError, match="app_env"):
        build_settings(app_env="prod")


def test_settings_reject_debug_in_production() -> None:
    with pytest.raises(ValidationError, match="debug"):
        build_production_settings(debug=True)


def test_settings_reject_wildcard_origin_with_credentials() -> None:
    with pytest.raises(ValidationError, match="allowed_origins"):
        build_settings(allowed_origins=["*"], allow_credentials=True)


def test_settings_allow_wildcard_origin_without_credentials() -> None:
    settings = build_settings(allowed_origins=["*"], allow_credentials=False)

    assert settings.allowed_origins == ["*"]
