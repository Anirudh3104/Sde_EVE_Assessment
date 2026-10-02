"""Pure unit tests for password hashing and JWT handling (no database or HTTP involved)."""

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.config import settings
from app.core.errors import UnauthorizedError
from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)


def test_password_hash_round_trip() -> None:
    hashed = hash_password("correct horse")

    assert hashed != "correct horse"
    assert verify_password("correct horse", hashed)
    assert not verify_password("wrong horse", hashed)


def test_password_hash_is_salted() -> None:
    assert hash_password("same-password") != hash_password("same-password")


def test_verify_password_with_malformed_hash_is_false() -> None:
    assert verify_password("anything", "not-a-bcrypt-hash") is False


def test_access_token_round_trip() -> None:
    user_id = uuid.uuid4()

    token, expires_in = create_access_token(user_id, "PATIENT")
    payload = decode_access_token(token)

    assert expires_in == settings.access_token_expire_minutes * 60
    assert payload["sub"] == str(user_id)
    assert payload["role"] == "PATIENT"
    assert payload["type"] == "access"


def _encode(**claims) -> str:
    return jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)


@pytest.mark.parametrize(
    ("token", "code"),
    [
        (
            _encode(sub="x", type="access", exp=datetime.now(UTC) - timedelta(seconds=1)),
            "token_expired",
        ),
        (_encode(sub="x", type="refresh"), "invalid_token"),
        (_encode(type="access"), "invalid_token"),
        (
            jwt.encode(
                {"sub": "x", "type": "access"},
                "a-different-secret-that-is-long-enough!!",
                algorithm="HS256",
            ),
            "invalid_token",
        ),
        ("garbage", "invalid_token"),
    ],
    ids=["expired", "wrong-type", "missing-sub", "wrong-signature", "garbage"],
)
def test_decode_rejects_bad_tokens(token: str, code: str) -> None:
    with pytest.raises(UnauthorizedError) as exc_info:
        decode_access_token(token)

    assert exc_info.value.code == code
