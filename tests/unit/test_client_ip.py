import ipaddress

import pytest

from app.core.client_ip import (
    UNKNOWN_CLIENT,
    parse_trusted_proxies,
    rate_limit_subject,
    resolve_client_ip,
)

pytestmark = pytest.mark.unit

PROXY = parse_trusted_proxies(["10.0.0.1"])
PROXY_SUBNET = parse_trusted_proxies(["10.0.0.0/24"])


# ----------------------------------------------------------------------------- resolve_client_ip
def test_forwarded_header_is_ignored_without_trusted_proxies() -> None:
    assert resolve_client_ip("203.0.113.9", "1.2.3.4", ()) == "203.0.113.9"


def test_forwarded_header_is_ignored_when_peer_is_not_a_trusted_proxy() -> None:
    assert resolve_client_ip("198.51.100.20", "1.2.3.4", PROXY) == "198.51.100.20"


def test_trusted_proxy_forwards_real_client_ip() -> None:
    assert resolve_client_ip("10.0.0.1", "203.0.113.5", PROXY) == "203.0.113.5"


def test_client_supplied_entries_left_of_the_proxy_hop_are_ignored() -> None:
    # Kẻ tấn công tự gửi "X-Forwarded-For: 6.6.6.6"; proxy nối thêm IP thật vào cuối
    assert resolve_client_ip("10.0.0.1", "6.6.6.6, 203.0.113.5", PROXY) == "203.0.113.5"


def test_chain_of_trusted_proxies_is_walked_from_the_right() -> None:
    assert resolve_client_ip("10.0.0.1", "203.0.113.5, 10.0.0.2", PROXY_SUBNET) == "203.0.113.5"


def test_invalid_hop_stops_at_the_nearest_trusted_hop() -> None:
    assert resolve_client_ip("10.0.0.1", "not-an-ip", PROXY) == "10.0.0.1"


def test_trusted_proxy_without_forwarded_header_is_the_client() -> None:
    assert resolve_client_ip("10.0.0.1", None, PROXY) == "10.0.0.1"


def test_ipv4_mapped_peer_is_matched_against_ipv4_proxies() -> None:
    assert resolve_client_ip("::ffff:10.0.0.1", "203.0.113.5", PROXY) == "203.0.113.5"


@pytest.mark.parametrize(
    ("peer", "expected"), [(None, UNKNOWN_CLIENT), ("testclient", "testclient")]
)
def test_non_ip_peers_are_returned_unchanged(peer: str | None, expected: str) -> None:
    assert resolve_client_ip(peer, "1.2.3.4", PROXY) == expected


# ----------------------------------------------------------------------------- rate_limit_subject
def test_ipv4_subject_is_the_address_itself() -> None:
    assert rate_limit_subject("203.0.113.5") == "203.0.113.5"


def test_ipv6_addresses_in_the_same_64_share_a_subject() -> None:
    first = rate_limit_subject("2001:db8:1:2::10")
    second = rate_limit_subject("2001:db8:1:2:ffff:ffff:ffff:ffff")

    assert first == second == "2001:db8:1:2::/64"


def test_ipv6_addresses_in_different_64_have_different_subjects() -> None:
    assert rate_limit_subject("2001:db8:1:2::10") != rate_limit_subject("2001:db8:1:3::10")


def test_ipv4_mapped_ipv6_subject_is_the_ipv4_address() -> None:
    assert rate_limit_subject("::ffff:203.0.113.5") == "203.0.113.5"


def test_non_ip_subject_is_returned_unchanged() -> None:
    assert rate_limit_subject(UNKNOWN_CLIENT) == UNKNOWN_CLIENT


# ------------------------------------------------------------------------- parse_trusted_proxies
def test_parse_trusted_proxies_accepts_addresses_and_networks() -> None:
    networks = parse_trusted_proxies(["10.0.0.5", "192.168.1.0/24", "2001:db8::/48"])

    assert networks == (
        ipaddress.ip_network("10.0.0.5/32"),
        ipaddress.ip_network("192.168.1.0/24"),
        ipaddress.ip_network("2001:db8::/48"),
    )


@pytest.mark.parametrize("entry", ["*", "0.0.0.0/0", "::/0"], ids=["star", "any-ipv4", "any-ipv6"])
def test_parse_trusted_proxies_rejects_wildcards(entry: str) -> None:
    with pytest.raises(ValueError, match="wildcard"):
        parse_trusted_proxies([entry])


def test_parse_trusted_proxies_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="không hợp lệ"):
        parse_trusted_proxies(["proxy.internal"])
