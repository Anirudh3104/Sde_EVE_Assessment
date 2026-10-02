"""Rate limiting with slowapi, backed by Redis (falls back to in-memory if Redis is down).

Usage on an endpoint (the endpoint must accept `request: Request`):

    @router.post("/login")
    @limiter.limit(lambda: settings.login_rate_limit)
    async def login(request: Request, ...): ...
"""

import jwt
from fastapi import Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.core.config import settings
from app.core.errors import error_body


def user_or_ip_key(request: Request) -> str:
    """Key by the authenticated user's id when a bearer token is present, else by client IP."""
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        try:
            payload = jwt.decode(auth[7:], settings.jwt_secret, algorithms=[settings.jwt_algorithm])
            return f"user:{payload['sub']}"
        except (jwt.InvalidTokenError, KeyError):
            pass
    return f"ip:{get_remote_address(request)}"


limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=settings.redis_url,
    in_memory_fallback_enabled=True,
    enabled=settings.rate_limit_enabled,
    headers_enabled=False,
)


async def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content=error_body("rate_limited", f"Rate limit exceeded: {exc.detail}", request),
    )
