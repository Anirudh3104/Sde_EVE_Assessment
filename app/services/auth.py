"""Account creation and credential checks."""

from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, UnauthorizedError
from app.core.security import create_access_token, hash_password, verify_password
from app.domain.enums import UserRole
from app.models.user import User
from app.schemas.auth import LoginRequest, SignupRequest, TokenResponse

_INVALID_CREDENTIALS = "Invalid email or password"


@lru_cache
def _dummy_password_hash() -> str:
    """A real bcrypt hash to verify against when the email is unknown, so that response time
    does not reveal whether an account exists."""
    return hash_password("not-a-real-password")


async def signup(db: AsyncSession, data: SignupRequest) -> User:
    """Create a PATIENT account. Emails arrive already normalised (stripped, lower-cased)."""
    existing = await db.scalar(select(User.id).where(User.email == data.email))
    if existing is not None:
        raise ConflictError("Email is already registered", code="email_taken")

    user = User(
        email=data.email,
        password_hash=hash_password(data.password),
        full_name=data.full_name,
        role=UserRole.PATIENT,
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError as exc:  # lost a race with a concurrent signup for the same email
        await db.rollback()
        raise ConflictError("Email is already registered", code="email_taken") from exc
    await db.refresh(user)
    return user


async def login(db: AsyncSession, data: LoginRequest) -> TokenResponse:
    """Exchange email + password for an access token. Unknown email and wrong password give
    the same error (no user enumeration)."""
    user = await db.scalar(select(User).where(User.email == data.email))
    if user is None:
        verify_password(data.password, _dummy_password_hash())
        raise UnauthorizedError(_INVALID_CREDENTIALS, code="invalid_credentials")
    if not verify_password(data.password, user.password_hash):
        raise UnauthorizedError(_INVALID_CREDENTIALS, code="invalid_credentials")

    token, expires_in = create_access_token(user.id, user.role)
    return TokenResponse(access_token=token, expires_in=expires_in)
