"""Giới hạn tần suất upload ảnh (P2-4). Cùng hai lớp như auth, KHÔNG sửa module auth:

1. Theo IP / mạng — in-memory, sức chứa riêng (`MEDIA_CAPACITY_SHARES`, được gộp với
   `AUTH_CAPACITY_SHARES` trong `create_app`): 60 lượt/giờ mỗi IPv4 hoặc IPv6 /64, mạng /48 dùng
   chung ngân sách gấp `IPV6_NETWORK_BUDGET_MULTIPLIER` lần. IPv4 và /48 fail-closed khi hết chỗ,
   chỉ /64 bỏ qua (vì /48 vẫn chặn).
2. Theo tài khoản — database (`AccountThrottle`, GCRA): 30 lượt/giờ mỗi user, dùng chung mọi
   worker, không bao giờ bỏ qua.

Mọi lượt upload đều bị tính, kể cả lượt bị từ chối (file sai định dạng, hết hạn mức…): đó vẫn là
công đọc / giải mã mà server phải bỏ ra.
"""

from app.core.client_ip import RateLimitSubject
from app.core.rate_limit import ClientRateLimit, RateLimitCheck, RateLimitRule, WhenFull
from app.modules.auth.rate_limits import IPV6_NETWORK_BUDGET_MULTIPLIER

HOUR = 60 * 60
UPLOAD_ACTION = "media-upload"
UPLOADS_PER_IP_PER_HOUR = 60
UPLOADS_PER_USER_PER_HOUR = 30

MEDIA_UPLOAD_LIMIT = ClientRateLimit(
    ipv4=RateLimitRule(UPLOADS_PER_IP_PER_HOUR, HOUR, name=f"{UPLOAD_ACTION}:ip"),
    ipv6_client=RateLimitRule(
        UPLOADS_PER_IP_PER_HOUR, HOUR, name=f"{UPLOAD_ACTION}:ip6", when_full=WhenFull.SKIP
    ),
    ipv6_network=RateLimitRule(
        UPLOADS_PER_IP_PER_HOUR * IPV6_NETWORK_BUDGET_MULTIPLIER,
        HOUR,
        name=f"{UPLOAD_ACTION}:net6",
    ),
)
MEDIA_UPLOAD_PER_USER = RateLimitRule(UPLOADS_PER_USER_PER_HOUR, HOUR, name=f"{UPLOAD_ACTION}:user")
MEDIA_UPLOAD_USER_RULES = (MEDIA_UPLOAD_PER_USER,)

# Tỉ trọng sức chứa của các rule IP media. Auth dùng tổng 100; phần media được cộng thêm, nên
# sau khi gộp mỗi rule auth giữ khoảng 100/110 phần cũ (vẫn có chỗ riêng, không dùng chung)
MEDIA_CAPACITY_SHARES: dict[RateLimitRule, int] = {
    MEDIA_UPLOAD_LIMIT.ipv4: 6,
    MEDIA_UPLOAD_LIMIT.ipv6_client: 2,
    MEDIA_UPLOAD_LIMIT.ipv6_network: 2,
}


def media_upload_checks(subject: RateLimitSubject) -> list[RateLimitCheck]:
    client_key = f"{UPLOAD_ACTION}:ip:{subject.client}"
    if subject.network is None:  # IPv4 (kể cả IPv4-mapped) hoặc địa chỉ không phải IP
        return [RateLimitCheck(client_key, MEDIA_UPLOAD_LIMIT.ipv4)]
    return [
        RateLimitCheck(client_key, MEDIA_UPLOAD_LIMIT.ipv6_client),
        RateLimitCheck(f"{UPLOAD_ACTION}:net:{subject.network}", MEDIA_UPLOAD_LIMIT.ipv6_network),
    ]


def media_user_subject(user_id: int) -> str:
    return f"user:{user_id}"
