from fastapi import APIRouter, Request, status

from app.core.config import settings
from app.core.deps import CurrentUser, DbSession
from app.core.rate_limit import limiter
from app.schemas.auth import LoginRequest, SignupRequest, TokenResponse, UserOut
from app.services import auth as auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/signup",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    summary="Register a patient account",
    responses={409: {"description": "Email is already registered"}},
)
@limiter.limit(lambda: settings.signup_rate_limit)
async def signup(request: Request, payload: SignupRequest, db: DbSession) -> UserOut:
    """Create a new account with role `PATIENT`. Emails are case-insensitive."""
    user = await auth_service.signup(db, payload)
    return UserOut.model_validate(user)


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Log in and get a bearer token",
    responses={401: {"description": "Invalid email or password"}},
)
@limiter.limit(lambda: settings.login_rate_limit)
async def login(request: Request, payload: LoginRequest, db: DbSession) -> TokenResponse:
    """Exchange email and password for a JWT access token.

    Send it as `Authorization: Bearer <access_token>` on subsequent requests.
    """
    return await auth_service.login(db, payload)


@router.get("/me", response_model=UserOut, summary="Get the current user")
async def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)
