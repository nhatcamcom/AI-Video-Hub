import pytest

from app.main import create_app
from app.modules.auth import security
from app.modules.auth.security import (
    generate_token,
    hash_password,
    hash_token,
    password_needs_rehash,
    token_matches_hash,
    verify_password,
    verify_password_against_dummy,
)
from tests.conftest import build_settings

pytestmark = pytest.mark.unit

PASSWORD = "correct-horse-battery-staple"


def test_hash_password_uses_argon2id_and_never_contains_plaintext() -> None:
    password_hash = hash_password(PASSWORD)

    assert password_hash.startswith("$argon2id$")
    assert PASSWORD not in password_hash


def test_hash_password_is_salted() -> None:
    assert hash_password(PASSWORD) != hash_password(PASSWORD)


def test_verify_password_accepts_correct_and_rejects_wrong_password() -> None:
    password_hash = hash_password(PASSWORD)

    assert verify_password(password_hash, PASSWORD) is True
    assert verify_password(password_hash, PASSWORD + "x") is False


def test_verify_password_rejects_malformed_hash_without_raising() -> None:
    assert verify_password("not-a-real-hash", PASSWORD) is False


def test_fresh_hash_does_not_need_rehash() -> None:
    assert password_needs_rehash(hash_password(PASSWORD)) is False


def test_dummy_verification_runs_without_error() -> None:
    verify_password_against_dummy(PASSWORD)  # chỉ để cân bằng thời gian, không trả gì


def test_create_app_prepares_dummy_hash_before_first_request() -> None:
    """Lần login đầu với email lạ không được chậm hơn bình thường (lộ email qua thời gian)."""
    security._dummy_password_hash.cache_clear()

    create_app(build_settings())

    assert security._dummy_password_hash.cache_info().currsize == 1


def test_generated_tokens_are_long_random_and_url_safe() -> None:
    tokens = {generate_token() for _ in range(50)}

    assert len(tokens) == 50
    assert all(len(token) >= 43 for token in tokens)  # 32 byte ngẫu nhiên → ≥ 43 ký tự base64url
    assert all(token.replace("-", "").replace("_", "").isalnum() for token in tokens)


def test_token_hash_is_deterministic_and_hides_the_token() -> None:
    token = generate_token()

    assert hash_token(token) == hash_token(token)
    assert token not in hash_token(token)
    assert len(hash_token(token)) == 64


def test_token_matches_hash_uses_exact_match() -> None:
    token = generate_token()

    assert token_matches_hash(token, hash_token(token)) is True
    assert token_matches_hash(token + "x", hash_token(token)) is False
