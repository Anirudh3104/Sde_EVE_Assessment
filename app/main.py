from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from slowapi.errors import RateLimitExceeded
from sqlalchemy import text

from app.api import auth, bookings, centres, payments, tests
from app.core.deps import DbSession
from app.core.errors import register_exception_handlers
from app.core.logging import RequestContextMiddleware, configure_logging
from app.core.rate_limit import limiter, rate_limit_exceeded_handler
from app.core.redis import close_redis, get_redis
from app.db.session import engine


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    yield
    await close_redis()
    await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="EVE Diagnostics API",
        version="0.1.0",
        description=(
            "Diagnostic test bookings with simulated payments and an idempotent payment webhook."
        ),
        lifespan=lifespan,
    )
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    register_exception_handlers(app)
    app.add_middleware(RequestContextMiddleware)

    for module in (auth, centres, tests, bookings, payments):
        app.include_router(module.router)

    @app.get("/health", tags=["health"])
    async def health(db: DbSession) -> dict[str, str]:
        await db.execute(text("SELECT 1"))
        try:
            await get_redis().ping()
            redis_status = "ok"
        except Exception:
            redis_status = "unavailable"
        return {"status": "ok", "database": "ok", "redis": redis_status}

    return app


app = create_app()
