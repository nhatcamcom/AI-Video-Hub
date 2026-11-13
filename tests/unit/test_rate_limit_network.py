"""Khóa mạng IPv6 /48 dùng kèm khóa client (/64) cho rate limit."""

import pytest

from app.core.client_ip import (
    UNKNOWN_CLIENT,
    RateLimitSubject,
    rate_limit_network,
    rate_limit_subject,
)
from app.core.rate_limit import ClientRateLimit, RateLimitRule, WhenFull
from app.modules.auth.rate_limits import (
    AUTH_CAPACITY_SHARES,
    IPV6_NETWORK_BUDGET_MULTIPLIER,
    LOGIN_LIMIT,
    REGISTER_LIMIT,
    forgot_password_checks,
    login_checks,
    register_checks,
    reset_password_checks,
)

pytestmark = pytest.mark.unit


def test_ipv6_addresses_in_the_same_48_share_a_network() -> None:
    assert rate_limit_network("2001:db8:aa:1::1") == "2001:db8:aa::/48"
    assert rate_limit_network("2001:db8:aa:ffff:ffff:ffff:ffff:ffff") == "2001:db8:aa::/48"


def test_different_48s_have_different_networks() -> None:
    assert rate_limit_network("2001:db8:aa::1") != rate_limit_network("2001:db8:ab::1")


def test_one_56_is_one_network_but_256_clients() -> None:
    clients = {rate_limit_subject(f"2001:db8:aa:{index:x}::1") for index in range(256)}
    networks = {rate_limit_network(f"2001:db8:aa:{index:x}::1") for index in range(256)}

    assert (len(clients), len(networks)) == (256, 1)


@pytest.mark.parametrize("client_ip", ["203.0.113.5", "::ffff:203.0.113.5", UNKNOWN_CLIENT, ""])
def test_ipv4_mapped_and_non_ip_clients_have_no_network(client_ip: str) -> None:
    assert rate_limit_network(client_ip) is None


def test_scoped_link_local_address_is_handled() -> None:
    assert rate_limit_network("fe80::1%eth0") == "fe80::/48"
    assert rate_limit_subject("fe80::1%eth0") == "fe80::/64"


def test_subject_combines_client_and_network() -> None:
    assert RateLimitSubject.from_client_ip("2001:db8:aa:1::9") == RateLimitSubject(
        client="2001:db8:aa:1::/64", network="2001:db8:aa::/48"
    )
    assert RateLimitSubject.from_client_ip("::ffff:198.51.100.1") == RateLimitSubject(
        client="198.51.100.1", network=None
    )


def test_ipv4_checks_use_only_the_client_rule() -> None:
    checks = register_checks(RateLimitSubject.from_client_ip("198.51.100.1"))

    assert [(check.key, check.rule) for check in checks] == [
        ("register:ip:198.51.100.1", REGISTER_LIMIT.ipv4)
    ]


def test_ipv6_checks_add_the_network_rule() -> None:
    checks = login_checks(RateLimitSubject.from_client_ip("2001:db8:aa:1::9"))

    assert [(check.key, check.rule) for check in checks] == [
        ("login:ip:2001:db8:aa:1::/64", LOGIN_LIMIT.ipv6_client),
        ("login:net:2001:db8:aa::/48", LOGIN_LIMIT.ipv6_network),
    ]


def test_network_budget_is_a_fixed_multiple_of_the_client_budget() -> None:
    for limit in (LOGIN_LIMIT, REGISTER_LIMIT):
        assert limit.ipv6_network.max_requests == (
            limit.ipv4.max_requests * IPV6_NETWORK_BUDGET_MULTIPLIER
        )
        assert limit.ipv6_client.max_requests == limit.ipv4.max_requests
        assert {limit.ipv6_client.window_seconds, limit.ipv6_network.window_seconds} == {
            limit.ipv4.window_seconds
        }


def test_only_the_ipv6_64_rule_skips_new_keys_when_full() -> None:
    for limit in (LOGIN_LIMIT, REGISTER_LIMIT):
        assert limit.ipv4.when_full is WhenFull.REJECT
        assert limit.ipv6_client.when_full is WhenFull.SKIP
        assert limit.ipv6_network.when_full is WhenFull.REJECT


@pytest.mark.parametrize(
    ("ipv4", "ipv6_client", "ipv6_network"),
    [
        (WhenFull.REJECT, WhenFull.REJECT, WhenFull.REJECT),
        (WhenFull.SKIP, WhenFull.SKIP, WhenFull.REJECT),
        (WhenFull.REJECT, WhenFull.SKIP, WhenFull.SKIP),
    ],
)
def test_client_limit_rejects_unsafe_overflow_policies(
    ipv4: WhenFull, ipv6_client: WhenFull, ipv6_network: WhenFull
) -> None:
    def rule(name: str, when_full: WhenFull) -> RateLimitRule:
        return RateLimitRule(max_requests=1, window_seconds=1, name=name, when_full=when_full)

    with pytest.raises(ValueError, match="khi đầy"):
        ClientRateLimit(
            ipv4=rule("a", ipv4),
            ipv6_client=rule("b", ipv6_client),
            ipv6_network=rule("c", ipv6_network),
        )


def test_every_rule_used_by_auth_endpoints_has_reserved_capacity() -> None:
    used = {
        check.rule
        for client_ip in ("198.51.100.1", "2001:db8:aa:1::9")
        for subject in [RateLimitSubject.from_client_ip(client_ip)]
        for checks in (
            login_checks(subject),
            register_checks(subject),
            forgot_password_checks(subject),
            reset_password_checks(subject),
        )
        for check in checks
    }

    assert used == set(AUTH_CAPACITY_SHARES)
    assert len({rule.name for rule in used}) == len(used)  # tên rule = pool riêng, không trùng
    assert sum(AUTH_CAPACITY_SHARES.values()) == 100
