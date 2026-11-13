import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from app.modules.users.models import User
from tests.helpers import DEFAULT_EMAIL, DEFAULT_PASSWORD, register_user

pytestmark = pytest.mark.integration

SENSITIVE_FIELDS = {"password", "password_hash", "hashed_password"}


async def test_register_creates_user_with_role_user(client: AsyncClient) -> None:
    response = await register_user(client)

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == DEFAULT_EMAIL
    assert body["full_name"] == "Alice Nguyễn"
    assert body["role"] == "user"
    assert body["is_active"] is True
    assert body["email_verified"] is False
    assert {"id", "created_at", "updated_at"} <= body.keys()
    assert SENSITIVE_FIELDS.isdisjoint(body.keys())


async def test_register_does_not_log_user_in(client: AsyncClient) -> None:
    await register_user(client)

    assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_register_normalizes_email(client: AsyncClient) -> None:
    response = await register_user(client, email="  Alice.Mixed@Example.COM ")

    assert response.status_code == 201
    assert response.json()["email"] == "alice.mixed@example.com"


async def test_register_rejects_duplicate_email(client: AsyncClient) -> None:
    await register_user(client)

    response = await register_user(client)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "email_already_registered"


async def test_register_rejects_duplicate_email_with_different_case(client: AsyncClient) -> None:
    await register_user(client, email="alice@example.com")

    response = await register_user(client, email="ALICE@EXAMPLE.COM")

    assert response.status_code == 409


@pytest.mark.parametrize(
    "email",
    ["not-an-email", "missing-domain@", "@example.com", "a@b", ""],
    ids=["no-at", "no-domain", "no-local", "no-tld", "empty"],
)
async def test_register_rejects_invalid_email(client: AsyncClient, email: str) -> None:
    response = await register_user(client, email=email)

    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["field"] == "body.email"


@pytest.mark.parametrize(
    "password",
    ["short", "x" * 9, " " * 12, "x" * 129],
    ids=["too-short", "nine-chars", "only-whitespace", "too-long"],
)
async def test_register_rejects_weak_password(client: AsyncClient, password: str) -> None:
    response = await register_user(client, password=password)

    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["field"] == "body.password"


@pytest.mark.parametrize("full_name", ["", "   ", "x" * 101], ids=["empty", "blank", "too-long"])
async def test_register_rejects_invalid_full_name(client: AsyncClient, full_name: str) -> None:
    response = await register_user(client, full_name=full_name)

    assert response.status_code == 422


async def test_register_stores_only_argon2id_hash(app: FastAPI, client: AsyncClient) -> None:
    await register_user(client)

    async with app.state.session_factory() as session:
        user = (await session.execute(select(User))).scalar_one()

    assert user.password_hash.startswith("$argon2id$")
    assert DEFAULT_PASSWORD not in user.password_hash


@pytest.mark.parametrize(
    "extra_field",
    [{"role": "admin"}, {"is_active": False}, {"email_verified": True}],
    ids=["role", "is-active", "email-verified"],
)
async def test_register_rejects_privileged_fields(
    app: FastAPI, client: AsyncClient, extra_field: dict[str, object]
) -> None:
    payload = {"email": DEFAULT_EMAIL, "password": DEFAULT_PASSWORD, "full_name": "Eve"}

    response = await client.post("/api/v1/auth/register", json={**payload, **extra_field})

    assert response.status_code == 422
    async with app.state.session_factory() as session:
        assert (await session.execute(select(User))).first() is None
