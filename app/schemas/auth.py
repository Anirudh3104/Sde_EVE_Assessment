import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.domain.enums import UserRole
from app.schemas.common import ORMModel


def _normalize_email(value: object) -> object:
    return value.strip().lower() if isinstance(value, str) else value


class SignupRequest(BaseModel):
    """New patient account. The role is never taken from the client: signups are PATIENTs."""

    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=1, max_length=200)

    _normalize = field_validator("email", mode="before")(_normalize_email)

    @field_validator("password")
    @classmethod
    def _password_fits_bcrypt(cls, value: str) -> str:
        # bcrypt only uses the first 72 bytes; reject longer input instead of silently truncating.
        if len(value.encode()) > 72:
            raise ValueError("must be at most 72 bytes when UTF-8 encoded")
        return value

    @field_validator("full_name")
    @classmethod
    def _strip_full_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class LoginRequest(BaseModel):
    # Plain `str`, not EmailStr: a malformed email is just a failed login (401), not a 422.
    email: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=128)

    _normalize = field_validator("email", mode="before")(_normalize_email)


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = Field(description="Token lifetime in seconds")


class UserOut(ORMModel):
    id: uuid.UUID
    email: str
    full_name: str
    role: UserRole
    created_at: datetime
