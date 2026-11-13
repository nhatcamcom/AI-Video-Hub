"""Profile người dùng và phân quyền USER / ADMIN."""

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from tests.helpers import csrf_headers, promote_to_admin, register_and_login, register_user

pytestmark = pytest.mark.integration


async def patch_me(
    client: AsyncClient, payload: dict[str, object]
) -> tuple[int, dict[str, object]]:
    response = await client.patch("/api/v1/users/me", json=payload, headers=csrf_headers(client))
    return response.status_code, response.json()


# ----------------------------------------------------------------------------- profile
async def test_update_profile_changes_full_name(client: AsyncClient) -> None:
    await register_and_login(client)

    status, body = await patch_me(client, {"full_name": "  Alice Trần  "})

    assert status == 200
    assert body["full_name"] == "Alice Trần"  # đã cắt khoảng trắng hai đầu
    assert (await client.get("/api/v1/auth/me")).json()["full_name"] == "Alice Trần"


async def test_update_profile_requires_authentication(client: AsyncClient) -> None:
    response = await client.patch("/api/v1/users/me", json={"full_name": "Bob"})

    assert response.status_code == 401


@pytest.mark.parametrize(
    "payload",
    [{"full_name": ""}, {"full_name": "   "}, {"full_name": "x" * 101}, {}],
    ids=["empty", "blank", "too-long", "missing"],
)
async def test_update_profile_rejects_invalid_input(
    client: AsyncClient, payload: dict[str, object]
) -> None:
    await register_and_login(client)

    status, _ = await patch_me(client, payload)

    assert status == 422


@pytest.mark.parametrize(
    "payload",
    [
        {"role": "admin"},
        {"full_name": "Eve", "role": "admin"},
        {"email": "other@example.com"},
        {"is_active": False},
        {"email_verified": True},
    ],
    ids=["role-only", "role-with-name", "email", "is-active", "email-verified"],
)
async def test_user_cannot_change_protected_fields_via_profile(
    client: AsyncClient, payload: dict[str, object]
) -> None:
    await register_and_login(client)

    status, _ = await patch_me(client, payload)

    assert status == 422
    me = (await client.get("/api/v1/auth/me")).json()
    assert me["role"] == "user"
    assert me["email"] == "alice@example.com"
    assert me["is_active"] is True


async def test_profile_response_hides_sensitive_fields(client: AsyncClient) -> None:
    await register_and_login(client)

    _, body = await patch_me(client, {"full_name": "Alice"})

    assert "password_hash" not in body
    assert "password" not in body


# ----------------------------------------------------------------------------- admin
async def test_admin_endpoint_requires_authentication(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/users")).status_code == 401


async def test_regular_user_cannot_access_admin_endpoint(client: AsyncClient) -> None:
    await register_and_login(client)

    response = await client.get("/api/v1/users")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


async def test_admin_can_list_users(app: FastAPI, client: AsyncClient) -> None:
    await register_user(client, email="bob@example.com")
    await register_and_login(client, email="admin@example.com")
    await promote_to_admin(app, "admin@example.com")

    response = await client.get("/api/v1/users", params={"limit": 10})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert [user["email"] for user in body["items"]] == ["bob@example.com", "admin@example.com"]
    assert all("password_hash" not in user for user in body["items"])


async def test_admin_user_list_is_paginated(app: FastAPI, client: AsyncClient) -> None:
    await register_user(client, email="bob@example.com")
    await register_and_login(client, email="admin@example.com")
    await promote_to_admin(app, "admin@example.com")

    page = (await client.get("/api/v1/users", params={"skip": 1, "limit": 1})).json()

    assert page["total"] == 2
    assert [user["email"] for user in page["items"]] == ["admin@example.com"]


@pytest.mark.parametrize("limit", ["0", "101"], ids=["zero", "over-max"])
async def test_admin_user_list_validates_limit(
    app: FastAPI, client: AsyncClient, limit: str
) -> None:
    await register_and_login(client, email="admin@example.com")
    await promote_to_admin(app, "admin@example.com")

    assert (await client.get("/api/v1/users", params={"limit": limit})).status_code == 422


async def test_promotion_takes_effect_for_existing_session(
    app: FastAPI, client: AsyncClient
) -> None:
    await register_and_login(client)
    assert (await client.get("/api/v1/users")).status_code == 403

    await promote_to_admin(app, "alice@example.com")

    assert (await client.get("/api/v1/users")).status_code == 200
