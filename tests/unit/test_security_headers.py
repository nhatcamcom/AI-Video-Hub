"""Chính sách header bảo mật theo đường dẫn và middleware ASGI (không qua HTTP)."""

import pytest
from starlette.types import Message, Receive, Scope, Send

from app.web.security_headers import (
    APP_CONTENT_SECURITY_POLICY,
    DOCS_CONTENT_SECURITY_POLICY,
    HSTS_HEADER_VALUE,
    SecurityHeadersMiddleware,
    SecurityHeadersPolicy,
)

pytestmark = pytest.mark.unit

DOCS_PATHS = frozenset({"/docs", "/redoc", "/docs/oauth2-redirect"})


def test_app_csp_has_no_unsafe_sources() -> None:
    assert "unsafe" not in APP_CONTENT_SECURITY_POLICY
    assert "*" not in APP_CONTENT_SECURITY_POLICY


def test_docs_csp_keeps_framing_plugin_and_eval_blocked() -> None:
    assert "frame-ancestors 'none'" in DOCS_CONTENT_SECURITY_POLICY
    assert "object-src 'none'" in DOCS_CONTENT_SECURITY_POLICY
    assert "base-uri 'self'" in DOCS_CONTENT_SECURITY_POLICY
    assert "unsafe-eval" not in DOCS_CONTENT_SECURITY_POLICY


@pytest.mark.parametrize("path", sorted(DOCS_PATHS))
def test_docs_paths_get_docs_csp_only_when_enabled(path: str) -> None:
    enabled = SecurityHeadersPolicy(is_hsts_enabled=False, docs_paths=DOCS_PATHS)
    disabled = SecurityHeadersPolicy(is_hsts_enabled=False)

    assert enabled.headers_for(path)["Content-Security-Policy"] == DOCS_CONTENT_SECURITY_POLICY
    assert disabled.headers_for(path)["Content-Security-Policy"] == APP_CONTENT_SECURITY_POLICY


def test_hsts_header_follows_policy_flag() -> None:
    with_hsts = SecurityHeadersPolicy(is_hsts_enabled=True).headers_for("/")
    without_hsts = SecurityHeadersPolicy(is_hsts_enabled=False).headers_for("/")

    assert with_hsts["Strict-Transport-Security"] == HSTS_HEADER_VALUE
    assert "Strict-Transport-Security" not in without_hsts


def test_headers_for_returns_independent_copies() -> None:
    policy = SecurityHeadersPolicy(is_hsts_enabled=False)

    first = policy.headers_for("/")
    first["X-Frame-Options"] = "ALLOWALL"

    assert policy.headers_for("/")["X-Frame-Options"] == "DENY"


async def test_middleware_passes_non_http_scopes_through_unchanged() -> None:
    received: list[Scope] = []
    sent: list[Message] = []

    async def inner_app(scope: Scope, receive: Receive, send: Send) -> None:
        received.append(scope)
        await send({"type": "lifespan.startup.complete"})

    async def receive() -> Message:
        return {"type": "lifespan.startup"}

    async def send(message: Message) -> None:
        sent.append(message)

    middleware = SecurityHeadersMiddleware(inner_app, SecurityHeadersPolicy(is_hsts_enabled=True))
    scope: Scope = {"type": "lifespan", "asgi": {"version": "3.0"}}

    await middleware(scope, receive, send)

    assert received == [scope]
    assert sent == [{"type": "lifespan.startup.complete"}]
