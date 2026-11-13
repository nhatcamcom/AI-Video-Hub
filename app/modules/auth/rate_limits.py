"""Ngưỡng giới hạn tần suất cho các endpoint xác thực (chống brute-force và spam).

Hai lớp bảo vệ, trách nhiệm tách bạch:

1. Theo IP / mạng — in-memory (`InMemoryRateLimiter`), kiểm tra trước, rẻ: chặn một nguồn gửi
   quá nhiều request. Mỗi rule có sức chứa riêng (`AUTH_CAPACITY_SHARES`). Khi hết chỗ:
   - IPv4 và mạng IPv6 /48: fail-closed (429).
   - IPv6 /64: bỏ qua /64 MỚI; /48 chứa nó vẫn chặn, nên không nguồn nào vượt ngân sách /48.
2. Theo tài khoản / thiết bị — trong database (`app/modules/auth/throttle.py`): chặn dò mật khẩu
   PHÂN TÁN vào một tài khoản và spam email reset. Không có sức chứa để làm đầy, không bao giờ
   bỏ qua, dùng chung mọi worker. Áp dụng như nhau cho email có thật hay không.
"""

from app.core.client_ip import RateLimitSubject
from app.core.rate_limit import ClientRateLimit, RateLimitCheck, RateLimitRule, WhenFull

MINUTE = 60
HOUR = 60 * MINUTE
DAY = 24 * HOUR

# Một mạng IPv6 /48 dùng chung ngân sách bằng 10 client: đủ rộng cho nhiều thuê bao/thiết bị
# sau cùng một mạng, nhưng một /56 (256 dải /64) không còn nhân ngân sách lên 256 lần
IPV6_NETWORK_BUDGET_MULTIPLIER = 10


def _client_limit(action: str, max_requests: int, window_seconds: int) -> ClientRateLimit:
    return ClientRateLimit(
        ipv4=RateLimitRule(max_requests, window_seconds, name=f"{action}:ip"),
        ipv6_client=RateLimitRule(
            max_requests, window_seconds, name=f"{action}:ip6", when_full=WhenFull.SKIP
        ),
        ipv6_network=RateLimitRule(
            max_requests * IPV6_NETWORK_BUDGET_MULTIPLIER, window_seconds, name=f"{action}:net6"
        ),
    )


# Theo IP: chặn một nguồn thử hàng loạt tài khoản khác nhau
LOGIN_LIMIT = _client_limit("login", max_requests=30, window_seconds=5 * MINUTE)
REGISTER_LIMIT = _client_limit("register", max_requests=5, window_seconds=HOUR)
FORGOT_PASSWORD_LIMIT = _client_limit("forgot", max_requests=10, window_seconds=HOUR)
RESET_PASSWORD_LIMIT = _client_limit("reset", max_requests=10, window_seconds=HOUR)

LOGIN_PER_IP = LOGIN_LIMIT.ipv4
REGISTER_PER_IP = REGISTER_LIMIT.ipv4
FORGOT_PASSWORD_PER_IP = FORGOT_PASSWORD_LIMIT.ipv4
RESET_PASSWORD_PER_IP = RESET_PASSWORD_LIMIT.ipv4

# --- Theo tài khoản / thiết bị (database, GCRA). Dồn tối đa `max_requests` lượt, sau đó hồi một
# lượt mỗi `window / max_requests` → khi bị dò liên tục: ~`max_requests` lượt mỗi cửa sổ, riêng
# cửa sổ đầu tiên tối đa gần gấp đôi (phần dồn + phần hồi). Giới hạn ngắn chặn dò nhanh, giới hạn
# ngày chặn dò chậm kéo dài. Đăng nhập thành công xóa bộ đếm.
LOGIN_PER_EMAIL = RateLimitRule(max_requests=10, window_seconds=15 * MINUTE, name="login:email")
LOGIN_PER_EMAIL_DAILY = RateLimitRule(max_requests=100, window_seconds=DAY, name="login:email:day")
# Trình duyệt đã từng đăng nhập thành công có ngân sách RIÊNG → kẻ tấn công đốt hết ngân sách
# theo email cũng không khóa được chủ tài khoản trên thiết bị quen
LOGIN_PER_DEVICE = RateLimitRule(max_requests=10, window_seconds=15 * MINUTE, name="login:device")
LOGIN_PER_DEVICE_DAILY = RateLimitRule(
    max_requests=100, window_seconds=DAY, name="login:device:day"
)
FORGOT_PASSWORD_PER_EMAIL = RateLimitRule(
    max_requests=3, window_seconds=15 * MINUTE, name="forgot:email"
)
FORGOT_PASSWORD_PER_EMAIL_DAILY = RateLimitRule(
    max_requests=10, window_seconds=DAY, name="forgot:email:day"
)

ACCOUNT_LOGIN_RULES = (LOGIN_PER_EMAIL, LOGIN_PER_EMAIL_DAILY)
DEVICE_LOGIN_RULES = (LOGIN_PER_DEVICE, LOGIN_PER_DEVICE_DAILY)
FORGOT_PASSWORD_RULES = (FORGOT_PASSWORD_PER_EMAIL, FORGOT_PASSWORD_PER_EMAIL_DAILY)

# Tỉ trọng chia RATE_LIMIT_MAX_KEYS (tổng 100 = phần trăm) cho các rule IP in-memory. Mỗi
# client/mạng chiếm đúng 1 bucket mỗi rule; IPv4 được nhiều nhất vì mỗi địa chỉ là một client.
AUTH_CAPACITY_SHARES: dict[RateLimitRule, int] = {
    LOGIN_LIMIT.ipv4: 22,
    LOGIN_LIMIT.ipv6_client: 8,
    LOGIN_LIMIT.ipv6_network: 7,
    REGISTER_LIMIT.ipv4: 13,
    REGISTER_LIMIT.ipv6_client: 4,
    REGISTER_LIMIT.ipv6_network: 4,
    FORGOT_PASSWORD_LIMIT.ipv4: 13,
    FORGOT_PASSWORD_LIMIT.ipv6_client: 4,
    FORGOT_PASSWORD_LIMIT.ipv6_network: 4,
    RESET_PASSWORD_LIMIT.ipv4: 13,
    RESET_PASSWORD_LIMIT.ipv6_client: 4,
    RESET_PASSWORD_LIMIT.ipv6_network: 4,
}


def _client_checks(
    action: str, subject: RateLimitSubject, limit: ClientRateLimit
) -> list[RateLimitCheck]:
    client_key = f"{action}:ip:{subject.client}"
    if subject.network is None:  # IPv4 (kể cả IPv4-mapped) hoặc địa chỉ không phải IP
        return [RateLimitCheck(client_key, limit.ipv4)]
    return [
        RateLimitCheck(client_key, limit.ipv6_client),
        RateLimitCheck(f"{action}:net:{subject.network}", limit.ipv6_network),
    ]


def login_checks(subject: RateLimitSubject) -> list[RateLimitCheck]:
    return _client_checks("login", subject, LOGIN_LIMIT)


def register_checks(subject: RateLimitSubject) -> list[RateLimitCheck]:
    return _client_checks("register", subject, REGISTER_LIMIT)


def forgot_password_checks(subject: RateLimitSubject) -> list[RateLimitCheck]:
    return _client_checks("forgot", subject, FORGOT_PASSWORD_LIMIT)


def reset_password_checks(subject: RateLimitSubject) -> list[RateLimitCheck]:
    return _client_checks("reset", subject, RESET_PASSWORD_LIMIT)
