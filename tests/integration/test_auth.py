from datetime import UTC, datetime, timedelta

import jwt
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from tests.conftest import TEST_PASSWORD, AuthedUser, UserFactory

SIGNUP = {"email": "Asha@Example.com", "password": "s3cure-pass", "full_name": "Asha Rao"}


def assert_error(response: Response, status: int, code: str | None = None) -> dict:
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert set(error) == {"code", "message", "details", "request_id"}
    if code is not None:
        assert error["code"] == code
    return error


# --- signup -----------------------------------------------------------------------------------


async def test_signup_creates_patient_with_normalised_email(client: AsyncClient) -> None:
    response = await client.post("/auth/signup", json={**SIGNUP, "email": "  Asha@Example.com "})

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "asha@example.com"
    assert body["full_name"] == "Asha Rao"
    assert body["role"] == "PATIENT"
    assert {"id", "created_at"} <= body.keys()
    assert "password" not in body and "password_hash" not in body


async def test_signup_ignores_client_supplied_role(client: AsyncClient) -> None:
    response = await client.post("/auth/signup", json={**SIGNUP, "role": "ADMIN"})

    assert response.status_code == 201
    assert response.json()["role"] == "PATIENT"


async def test_signup_duplicate_email_is_case_insensitive(client: AsyncClient) -> None:
    await client.post("/auth/signup", json=SIGNUP)

    response = await client.post("/auth/signup", json={**SIGNUP, "email": "ASHA@example.COM"})

    assert_error(response, 409, "email_taken")


async def test_signup_rejects_invalid_email(client: AsyncClient) -> None:
    response = await client.post("/auth/signup", json={**SIGNUP, "email": "not-an-email"})

    error = assert_error(response, 422, "validation_error")
    assert error["details"][0]["loc"] == ["body", "email"]


async def test_signup_rejects_short_password(client: AsyncClient) -> None:
    response = await client.post("/auth/signup", json={**SIGNUP, "password": "short"})

    error = assert_error(response, 422, "validation_error")
    assert error["details"][0]["loc"] == ["body", "password"]


# --- login ------------------------------------------------------------------------------------


async def test_login_returns_bearer_token(client: AsyncClient, patient: AuthedUser) -> None:
    response = await client.post(
        "/auth/login", json={"email": patient.user.email.upper(), "password": TEST_PASSWORD}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == settings.access_token_expire_minutes * 60

    me = await client.get("/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200
    assert me.json()["id"] == str(patient.user.id)


async def test_login_wrong_password_and_unknown_email_look_the_same(
    client: AsyncClient, patient: AuthedUser
) -> None:
    wrong_password = await client.post(
        "/auth/login", json={"email": patient.user.email, "password": "wrong-password"}
    )
    unknown_email = await client.post(
        "/auth/login", json={"email": "nobody@example.com", "password": TEST_PASSWORD}
    )

    first = assert_error(wrong_password, 401, "invalid_credentials")
    second = assert_error(unknown_email, 401, "invalid_credentials")
    assert first["message"] == second["message"] == "Invalid email or password"


# --- /auth/me ---------------------------------------------------------------------------------


async def test_me_returns_current_user(client: AsyncClient, admin: AuthedUser) -> None:
    response = await client.get("/auth/me", headers=admin.headers)

    assert response.status_code == 200
    assert response.json()["email"] == admin.user.email
    assert response.json()["role"] == "ADMIN"


async def test_me_requires_token(client: AsyncClient) -> None:
    response = await client.get("/auth/me")

    assert_error(response, 401, "not_authenticated")
    assert response.headers["WWW-Authenticate"] == "Bearer"


async def test_me_rejects_garbage_token(client: AsyncClient) -> None:
    response = await client.get("/auth/me", headers={"Authorization": "Bearer not.a.jwt"})

    assert_error(response, 401, "invalid_token")


async def test_me_rejects_token_signed_with_another_secret(
    client: AsyncClient, patient: AuthedUser
) -> None:
    token = jwt.encode(
        {
            "sub": str(patient.user.id),
            "type": "access",
            "exp": datetime.now(UTC) + timedelta(hours=1),
        },
        "a-different-secret-that-is-long-enough!!",
        algorithm=settings.jwt_algorithm,
    )

    response = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert_error(response, 401, "invalid_token")


async def test_me_rejects_expired_token(client: AsyncClient, patient: AuthedUser) -> None:
    past = datetime.now(UTC) - timedelta(hours=1)
    token = jwt.encode(
        {"sub": str(patient.user.id), "type": "access", "iat": past, "exp": past},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )

    response = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert_error(response, 401, "token_expired")


async def test_me_rejects_token_of_deleted_user(
    client: AsyncClient, make_user: UserFactory, db: AsyncSession
) -> None:
    ghost = await make_user()
    await db.delete(ghost.user)
    await db.commit()

    response = await client.get("/auth/me", headers=ghost.headers)

    assert_error(response, 401, "invalid_token")
