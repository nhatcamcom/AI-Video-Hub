"""Xác định IP thật của client và "chủ thể" dùng cho rate limit.

Hai chế độ (cấu hình qua TRUSTED_PROXY_IPS):

1. Không có reverse proxy (mặc định, danh sách rỗng): X-Forwarded-For bị BỎ QUA hoàn toàn;
   IP là địa chỉ kết nối TCP trực tiếp.
2. Có reverse proxy: chỉ khi kết nối trực tiếp đến từ một proxy đã khai báo thì mới đọc
   X-Forwarded-For, duyệt từ PHẢI sang trái và dừng ở hop đầu tiên không phải proxy. Giá trị do
   client tự chèn nằm bên trái hop đó nên không thể giả mạo.

Uvicorn phải chạy với `--no-proxy-headers` (hoặc dùng `python -m app.server`), nếu không chính
uvicorn sẽ ghi đè địa chỉ client từ header trước khi request tới app.
"""

import ipaddress
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

UNKNOWN_CLIENT = "unknown"
IPV6_RATE_LIMIT_PREFIX = 64  # một người dùng IPv6 thường sở hữu cả một dải /64
# Một khách hàng/site thường được cấp tới /48 (65.536 dải /64): giới hạn chung cho cả mạng để
# người giữ /56 hay /48 không nhân được ngân sách lên hàng trăm, hàng nghìn lần
IPV6_NETWORK_RATE_LIMIT_PREFIX = 48
# Dải proxy CÔNG KHAI rộng hơn mức này gần như là wildcard (vd. 0.0.0.0/1 = nửa Internet). Dải nội
# bộ (10.0.0.0/8, fd00::/8) và dải CDN công bố (Cloudflare 104.16.0.0/13, 2400:cb00::/32) vẫn hợp lệ
_MIN_TRUSTED_PREFIX = {4: 8, 6: 32}

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_ip(value: str) -> IPAddress | None:
    """Parse địa chỉ IP; IPv4-mapped IPv6 (::ffff:a.b.c.d) được quy về IPv4."""
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def parse_trusted_proxies(entries: Iterable[str]) -> tuple[IPNetwork, ...]:
    networks: list[IPNetwork] = []
    for entry in entries:
        try:
            network = ipaddress.ip_network(entry.strip(), strict=False)
        except ValueError as exc:
            if entry.strip() == "*":
                raise ValueError("không được dùng wildcard '*' cho trusted proxy") from exc
            raise ValueError(f"địa chỉ trusted proxy không hợp lệ: {entry!r}") from exc
        if network.prefixlen == 0:
            raise ValueError(f"không được dùng wildcard {entry!r} cho trusted proxy")
        if network.prefixlen < _MIN_TRUSTED_PREFIX[network.version] and not network.is_private:
            raise ValueError(
                f"dải trusted proxy {entry!r} quá rộng (tối thiểu /"
                f"{_MIN_TRUSTED_PREFIX[network.version]}): chỉ khai báo đúng dải của proxy"
            )
        networks.append(network)
    return tuple(networks)


def _is_trusted(address: IPAddress, trusted: Sequence[IPNetwork]) -> bool:
    return any(address in network for network in trusted)


def resolve_client_ip(
    peer: str | None, forwarded_for: str | None, trusted: Sequence[IPNetwork]
) -> str:
    """IP client thật, chỉ tin X-Forwarded-For khi peer là proxy đã khai báo."""
    if peer is None:
        return UNKNOWN_CLIENT
    candidate = parse_ip(peer)
    if candidate is None:
        return peer
    if not trusted or not _is_trusted(candidate, trusted):
        return str(candidate)

    hops = (forwarded_for or "").split(",")
    for hop in reversed(hops):
        if not _is_trusted(candidate, trusted):
            break
        hop_address = parse_ip(hop)
        if hop_address is None:
            break  # rác hoặc giả mạo → dừng ở hop tin cậy gần nhất (an toàn: gộp vào proxy)
        candidate = hop_address
    return str(candidate)


def _ipv6_network(address: ipaddress.IPv6Address, prefix: int) -> str:
    # Bỏ scope id (fe80::1%eth0) trước khi lấy mạng; chỉ phần địa chỉ quyết định khóa
    return str(ipaddress.IPv6Network((int(address), prefix), strict=False))


def rate_limit_subject(client_ip: str) -> str:
    """Khóa rate limit: IPv4 theo từng địa chỉ, IPv6 theo prefix /64."""
    address = parse_ip(client_ip)
    if address is None:
        return client_ip
    if isinstance(address, ipaddress.IPv6Address):
        return _ipv6_network(address, IPV6_RATE_LIMIT_PREFIX)
    return str(address)


def rate_limit_network(client_ip: str) -> str | None:
    """Mạng IPv6 /48 chứa client; None với IPv4 (kể cả IPv4-mapped) và giá trị không phải IP."""
    address = parse_ip(client_ip)
    if not isinstance(address, ipaddress.IPv6Address):
        return None
    return _ipv6_network(address, IPV6_NETWORK_RATE_LIMIT_PREFIX)


@dataclass(frozen=True)
class RateLimitSubject:
    """Chủ thể của rate limit: client (IPv4 / IPv6 /64) và mạng IPv6 /48 chứa nó."""

    client: str
    network: str | None

    @classmethod
    def from_client_ip(cls, client_ip: str) -> "RateLimitSubject":
        return cls(client=rate_limit_subject(client_ip), network=rate_limit_network(client_ip))
