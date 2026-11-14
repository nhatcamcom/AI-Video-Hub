"""Giới hạn tần suất tạo video (P2-6, quyết định D5). Cùng hai lớp như media, KHÔNG sửa module auth:

1. Theo IP / mạng — in-memory, sức chứa riêng (`VIDEO_CAPACITY_SHARES`, gộp vào
   `APP_CAPACITY_SHARES` trong `create_app`): 60 lượt/giờ mỗi IPv4 hoặc IPv6 /64, mạng /48 dùng
   chung ngân sách gấp `IPV6_NETWORK_BUDGET_MULTIPLIER` lần. IPv4 và /48 fail-closed khi hết chỗ,
   chỉ /64 bỏ qua (vì /48 vẫn chặn).
2. Theo tài khoản — database (`AccountThrottle`, GCRA): 30 lượt/giờ mỗi user.

Mọi lượt đều bị tính, kể cả lượt bị từ chối (model sai, hết slot, hết hạn mức…).
"""

from app.core.client_ip import RateLimitSubject
from app.core.rate_limit import ClientRateLimit, RateLimitCheck, RateLimitRule, WhenFull
from app.modules.auth.rate_limits import IPV6_NETWORK_BUDGET_MULTIPLIER

HOUR = 60 * 60
CREATE_ACTION = "video-create"
CREATES_PER_IP_PER_HOUR = 60
CREATES_PER_USER_PER_HOUR = 30

VIDEO_CREATE_LIMIT = ClientRateLimit(
    ipv4=RateLimitRule(CREATES_PER_IP_PER_HOUR, HOUR, name=f"{CREATE_ACTION}:ip"),
    ipv6_client=RateLimitRule(
        CREATES_PER_IP_PER_HOUR, HOUR, name=f"{CREATE_ACTION}:ip6", when_full=WhenFull.SKIP
    ),
    ipv6_network=RateLimitRule(
        CREATES_PER_IP_PER_HOUR * IPV6_NETWORK_BUDGET_MULTIPLIER,
        HOUR,
        name=f"{CREATE_ACTION}:net6",
    ),
)
VIDEO_CREATE_PER_USER = RateLimitRule(CREATES_PER_USER_PER_HOUR, HOUR, name=f"{CREATE_ACTION}:user")
VIDEO_CREATE_USER_RULES = (VIDEO_CREATE_PER_USER,)

# Tỉ trọng sức chứa của các rule IP tạo video (tương đối, gộp với auth và media ở app/main.py)
VIDEO_CAPACITY_SHARES: dict[RateLimitRule, int] = {
    VIDEO_CREATE_LIMIT.ipv4: 6,
    VIDEO_CREATE_LIMIT.ipv6_client: 2,
    VIDEO_CREATE_LIMIT.ipv6_network: 2,
}


def video_create_checks(subject: RateLimitSubject) -> list[RateLimitCheck]:
    client_key = f"{CREATE_ACTION}:ip:{subject.client}"
    if subject.network is None:  # IPv4 (kể cả IPv4-mapped) hoặc địa chỉ không phải IP
        return [RateLimitCheck(client_key, VIDEO_CREATE_LIMIT.ipv4)]
    return [
        RateLimitCheck(client_key, VIDEO_CREATE_LIMIT.ipv6_client),
        RateLimitCheck(f"{CREATE_ACTION}:net:{subject.network}", VIDEO_CREATE_LIMIT.ipv6_network),
    ]


def video_user_subject(user_id: int) -> str:
    return f"user:{user_id}"
