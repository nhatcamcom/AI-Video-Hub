"""Băm mật khẩu (Argon2id) và token ngẫu nhiên.

Các hàm băm mật khẩu tốn CPU (~50-100 ms) và RAM (64 MiB mỗi lượt) có chủ đích. Code async gọi bản
`*_async`: chạy trên một thread pool RIÊNG có số luồng cố định, nên dù bao nhiêu request đăng nhập
cùng lúc thì RAM cho Argon2 vẫn không vượt PASSWORD_HASH_MAX_CONCURRENCY x 64 MiB (phần còn lại
xếp hàng chờ).
"""

import asyncio
import hashlib
import hmac
import secrets
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

# Argon2id với tham số mặc định của argon2-cffi (RFC 9106, cấu hình bộ nhớ thấp: 64 MiB, t=3, p=4)
_PASSWORD_HASHER = PasswordHasher()

TOKEN_BYTES = 32  # 256 bit ngẫu nhiên → không thể đoán / brute-force
PASSWORD_HASH_MAX_CONCURRENCY = 4  # 4 x 64 MiB = 256 MiB trần RAM cho Argon2 mỗi tiến trình

_PASSWORD_HASH_EXECUTOR = ThreadPoolExecutor(
    max_workers=PASSWORD_HASH_MAX_CONCURRENCY, thread_name_prefix="argon2"
)


async def _run_password_hashing[Result](function: Callable[..., Result], *args: str) -> Result:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_PASSWORD_HASH_EXECUTOR, function, *args)


def hash_password(password: str) -> str:
    return _PASSWORD_HASHER.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _PASSWORD_HASHER.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    """True khi tham số băm đã được nâng cấp kể từ lúc tạo hash này."""
    return _PASSWORD_HASHER.check_needs_rehash(password_hash)


@cache
def _dummy_password_hash() -> str:
    return hash_password(secrets.token_urlsafe(16))


def prepare_password_hashing() -> None:
    """Tính sẵn dummy hash lúc khởi động.

    Nếu tính lười ở request đầu tiên, lần login đầu với email lạ sẽ chậm gấp đôi → lộ email.
    """
    _dummy_password_hash()


def verify_password_against_dummy(password: str) -> None:
    """Tốn thời gian như một lần kiểm tra thật khi email không tồn tại.

    Chống dò email qua thời gian phản hồi.
    """
    verify_password(_dummy_password_hash(), password)


def generate_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    # Token đã có 256 bit entropy nên SHA-256 là đủ (khác mật khẩu, không cần hàm băm chậm)
    return hashlib.sha256(token.encode()).hexdigest()


def token_matches_hash(token: str, expected_hash: str) -> bool:
    return hmac.compare_digest(hash_token(token), expected_hash)


async def hash_password_async(password: str) -> str:
    return await _run_password_hashing(hash_password, password)


async def verify_password_async(password_hash: str, password: str) -> bool:
    return await _run_password_hashing(verify_password, password_hash, password)


async def verify_password_against_dummy_async(password: str) -> None:
    await _run_password_hashing(verify_password_against_dummy, password)
