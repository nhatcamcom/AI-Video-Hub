"""Cấu hình nguy hiểm phải bị từ chối ngay khi khởi động (L-3, L-5, L-6, khóa HMAC)."""

import pytest
from pydantic import ValidationError

from app.core.client_ip import parse_trusted_proxies
from tests.conftest import SettingValue, build_settings

pytestmark = pytest.mark.unit

THROTTLE_KEY = "k" * 48
HARDENED_BASE: dict[str, SettingValue] = {
    "cookie_secure": True,
    "public_base_url": "https://app.example.com",
    "email_backend": "disabled",
    "auth_throttle_key": THROTTLE_KEY,
}


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_hardened_environments_reject_disabled_rate_limiting(app_env: str) -> None:
    with pytest.raises(ValidationError, match="rate_limit_enabled"):
        build_settings(app_env=app_env, **HARDENED_BASE, rate_limit_enabled=False)


@pytest.mark.parametrize("backend", ["file", "memory"])
def test_staging_rejects_email_backends_that_store_reset_tokens(backend: str) -> None:
    with pytest.raises(ValidationError, match="email_backend"):
        build_settings(app_env="staging", **{**HARDENED_BASE, "email_backend": backend})


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_hardened_environments_require_a_throttle_key(app_env: str) -> None:
    without_key = {
        name: value for name, value in HARDENED_BASE.items() if name != "auth_throttle_key"
    }

    with pytest.raises(ValidationError, match="auth_throttle_key"):
        build_settings(app_env=app_env, **without_key)


def test_hardened_environments_reject_a_short_throttle_key() -> None:
    with pytest.raises(ValidationError, match="auth_throttle_key"):
        build_settings(app_env="production", **{**HARDENED_BASE, "auth_throttle_key": "short"})


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_hardened_environments_accept_safe_settings(app_env: str) -> None:
    assert build_settings(app_env=app_env, **HARDENED_BASE).is_hardened


def test_throttle_key_is_not_exposed_in_repr() -> None:
    settings = build_settings(auth_throttle_key=THROTTLE_KEY)

    assert THROTTLE_KEY not in repr(settings)
    assert THROTTLE_KEY not in str(settings.model_dump())


@pytest.mark.parametrize("entry", ["0.0.0.0/1", "::/1", "0.0.0.0/7", "2000::/3", "128.0.0.0/2"])
def test_trusted_proxies_reject_near_wildcard_networks(entry: str) -> None:
    with pytest.raises(ValueError, match="quá rộng"):
        parse_trusted_proxies([entry])


@pytest.mark.parametrize(
    "entry", ["10.0.0.0/8", "192.168.0.0/16", "104.16.0.0/13", "2400:cb00::/32", "fd00::/8"]
)
def test_trusted_proxies_accept_private_and_cdn_sized_networks(entry: str) -> None:
    assert len(parse_trusted_proxies([entry])) == 1
