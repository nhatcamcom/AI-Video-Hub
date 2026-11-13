"""Đăng nhập, phiên làm việc, đăng xuất, CSRF và thuộc tính cookie."""

from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from app.main import create_app
from app.modules.auth.models import UserSession
from app.modules.auth.security import hash_token
from app.modules.users.models import User
from tests.conftest import build_production_settings, create_schema, sqlite_file_url
from tests.helpers import (
    CSRF_COOKIE,
    CSRF_HEADER,
    DEFAULT_EMAIL,
    DEFAULT_PASSWORD,
    SESSION_COOKIE,
    csrf_headers,
    login_user,
    register_and_login,
    register_user,
)

pytestmark = pytest.mark.integration

GENERIC_LOGIN_ERROR = "invalid_credentials"


def set_cookie_headers(response_headers: list[str], name: str) -> str:
    matches = [header for header in response_headers if header.startswith(f"{name}=")]
    assert len(matches) == 1, f"không thấy Set-Cookie cho {name}"
    return matches[0].lower()


# ----------------------------------------------------------------------------- đăng nhập
async def test_login_with_correct_credentials_returns_user_and_sets_cookies(
    client: AsyncClient,
) -> None:
    await register_user(client)

    response = await login_user(client)

    assert response.status_code == 200
    assert response.json()["email"] == DEFAULT_EMAIL
    assert SESSION_COOKIE in client.cookies
    assert CSRF_COOKIE in client.cookies
    assert "password_hash" not in response.json()


async def test_login_is_case_insensitive_on_email(client: AsyncClient) -> None:
    await register_user(client)

    response = await login_user(client, email="ALICE@Example.com")

    assert response.status_code == 200


async def test_login_with_wrong_password_returns_generic_401(client: AsyncClient) -> None:
    await register_user(client)

    response = await login_user(client, password="wrong-password-123")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == GENERIC_LOGIN_ERROR
    assert SESSION_COOKIE not in client.cookies


async def test_login_error_does_not_reveal_whether_email_exists(client: AsyncClient) -> None:
    await register_user(client)

    wrong_password = await login_user(client, password="wrong-password-123")
    unknown_email = await login_user(client, email="nobody@example.com")

    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json()


async def test_login_rejected_for_inactive_user(app: FastAPI, client: AsyncClient) -> None:
    await register_user(client)
    async with app.state.session_factory() as session:
        await session.execute(update(User).values(is_active=False))
        await session.commit()

    response = await login_user(client)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == GENERIC_LOGIN_ERROR


async def test_login_stores_only_hashes_of_session_and_csrf_tokens(
    app: FastAPI, client: AsyncClient
) -> None:
    await register_and_login(client)
    session_token = client.cookies[SESSION_COOKIE]
    csrf_token = client.cookies[CSRF_COOKIE]

    async with app.state.session_factory() as session:
        stored = (await session.execute(select(UserSession))).scalar_one()

    assert stored.token_hash == hash_token(session_token)
    assert stored.csrf_token_hash == hash_token(csrf_token)
    assert stored.revoked_at is None
    assert stored.expires_at > stored.created_at


async def test_new_login_replaces_existing_session(app: FastAPI, client: AsyncClient) -> None:
    await register_and_login(client)
    first_token = client.cookies[SESSION_COOKIE]

    await login_user(client)

    assert client.cookies[SESSION_COOKIE] != first_token
    async with app.state.session_factory() as session:
        first = (
            await session.execute(
                select(UserSession).where(UserSession.token_hash == hash_token(first_token))
            )
        ).scalar_one()
    assert first.revoked_at is not None  # chống session fixation


# ----------------------------------------------------------------------------- /auth/me
async def test_me_requires_authentication(client: AsyncClient) -> None:
    response = await client.get("/api/v1/auth/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_me_returns_current_user(client: AsyncClient) -> None:
    await register_and_login(client)

    response = await client.get("/api/v1/auth/me")

    assert response.status_code == 200
    assert response.json()["email"] == DEFAULT_EMAIL


async def test_me_rejects_forged_session_token(client: AsyncClient) -> None:
    client.cookies.set(SESSION_COOKIE, "forged-token-value-that-was-never-issued")

    assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_expired_session_is_rejected(app: FastAPI, client: AsyncClient) -> None:
    await register_and_login(client)
    async with app.state.session_factory() as session:
        stored = (await session.execute(select(UserSession))).scalar_one()
        stored.expires_at = stored.created_at  # đã hết hạn
        await session.commit()

    assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_session_of_deactivated_user_is_rejected(app: FastAPI, client: AsyncClient) -> None:
    await register_and_login(client)
    async with app.state.session_factory() as session:
        await session.execute(update(User).values(is_active=False))
        await session.commit()

    assert (await client.get("/api/v1/auth/me")).status_code == 401


# ----------------------------------------------------------------------------- đăng xuất
async def test_logout_revokes_session_and_clears_cookies(app: FastAPI, client: AsyncClient) -> None:
    await register_and_login(client)

    response = await client.post("/api/v1/auth/logout", headers=csrf_headers(client))

    assert response.status_code == 204
    assert SESSION_COOKIE not in client.cookies
    assert CSRF_COOKIE not in client.cookies
    async with app.state.session_factory() as session:
        stored = (await session.execute(select(UserSession))).scalar_one()
    assert stored.revoked_at is not None


async def test_old_session_token_cannot_be_reused_after_logout(client: AsyncClient) -> None:
    await register_and_login(client)
    old_session_token = client.cookies[SESSION_COOKIE]
    await client.post("/api/v1/auth/logout", headers=csrf_headers(client))

    client.cookies.set(SESSION_COOKIE, old_session_token)  # kẻ tấn công giữ lại cookie cũ

    assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_logout_without_session_still_succeeds(client: AsyncClient) -> None:
    response = await client.post("/api/v1/auth/logout")

    assert response.status_code == 204


# ----------------------------------------------------------------------------- CSRF
async def test_logout_requires_csrf_token(client: AsyncClient) -> None:
    await register_and_login(client)

    response = await client.post("/api/v1/auth/logout")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"
    assert (await client.get("/api/v1/auth/me")).status_code == 200  # session vẫn còn


async def test_state_changing_request_rejects_wrong_csrf_token(client: AsyncClient) -> None:
    await register_and_login(client)

    response = await client.patch(
        "/api/v1/users/me",
        json={"full_name": "Hacked"},
        headers={CSRF_HEADER: "attacker-guessed-value"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"


async def test_csrf_token_of_another_session_is_rejected(app: FastAPI) -> None:
    """Token CSRF gắn với đúng session phát hành nó, không dùng chéo được."""
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as alice,
        AsyncClient(transport=transport, base_url="http://test") as mallory,
    ):
        await register_and_login(alice, email="alice@example.com")
        await register_and_login(mallory, email="mallory@example.com")

        response = await alice.patch(
            "/api/v1/users/me", json={"full_name": "Hacked"}, headers=csrf_headers(mallory)
        )

    assert response.status_code == 403


@pytest.mark.parametrize(
    "content_type",
    ["text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x"],
    ids=["text-plain", "urlencoded", "multipart"],
)
async def test_login_rejects_non_json_bodies_against_login_csrf(
    client: AsyncClient, content_type: str
) -> None:
    """Trang độc hại chỉ gửi được 'simple request' (form/text) cross-site mà không cần preflight.

    Login/register chỉ nhận application/json → kẻ tấn công không thể ép trình duyệt nạn nhân
    đăng nhập vào tài khoản của chúng (login CSRF). JSON cross-site thì bị CORS chặn ở preflight.
    """
    await register_user(client)
    body = f'{{"email": "{DEFAULT_EMAIL}", "password": "{DEFAULT_PASSWORD}"}}'

    response = await client.post(
        "/api/v1/auth/login", content=body, headers={"Content-Type": content_type}
    )

    assert response.status_code == 422
    assert SESSION_COOKIE not in client.cookies


async def test_safe_methods_do_not_require_csrf(client: AsyncClient) -> None:
    await register_and_login(client)

    assert (await client.get("/api/v1/auth/me")).status_code == 200


# ----------------------------------------------------------------------------- cookie
async def test_cookies_are_httponly_samesite_and_not_secure_in_development(
    client: AsyncClient,
) -> None:
    await register_user(client)

    response = await login_user(client)

    headers = response.headers.get_list("set-cookie")
    session_cookie = set_cookie_headers(headers, SESSION_COOKIE)
    csrf_cookie = set_cookie_headers(headers, CSRF_COOKIE)
    assert "httponly" in session_cookie
    assert "samesite=lax" in session_cookie
    assert "path=/" in session_cookie
    assert "max-age=" in session_cookie
    assert "secure" not in session_cookie  # http://localhost khi dev
    assert "httponly" not in csrf_cookie  # JS cần đọc để gửi header X-CSRF-Token
    assert "samesite=lax" in csrf_cookie


async def test_cookies_are_secure_in_production(tmp_path: Path) -> None:
    app = create_app(build_production_settings(database_url=sqlite_file_url(tmp_path)))
    await create_schema(app)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with AsyncClient(transport=transport, base_url="https://test") as client:
            await register_user(client)
            response = await login_user(client)
    finally:
        await app.state.engine.dispose()

    assert response.status_code == 200
    headers = response.headers.get_list("set-cookie")
    # __Host-: trình duyệt chỉ nhận khi có Secure, Path=/ và KHÔNG có Domain → subdomain
    # không thể ghi đè cookie phiên (chống cookie tossing / session fixation)
    session_cookie = set_cookie_headers(headers, f"__Host-{SESSION_COOKIE}")
    assert "secure" in session_cookie
    assert "httponly" in session_cookie
    assert "path=/" in session_cookie
    assert "domain=" not in session_cookie
    assert "secure" in set_cookie_headers(headers, CSRF_COOKIE)


async def test_host_prefixed_session_cookie_authenticates_in_production(tmp_path: Path) -> None:
    app = create_app(build_production_settings(database_url=sqlite_file_url(tmp_path)))
    await create_schema(app)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with AsyncClient(transport=transport, base_url="https://test") as client:
            await register_and_login(client)
            me = await client.get("/api/v1/auth/me")
            logout = await client.post("/api/v1/auth/logout", headers=csrf_headers(client))
            after_logout = await client.get("/api/v1/auth/me")
    finally:
        await app.state.engine.dispose()

    assert me.status_code == 200
    assert logout.status_code == 204
    assert after_logout.status_code == 401


async def test_login_does_not_put_session_token_in_response_body(client: AsyncClient) -> None:
    await register_user(client)

    response = await login_user(client)

    assert client.cookies[SESSION_COOKIE] not in response.text
    assert DEFAULT_PASSWORD not in response.text
