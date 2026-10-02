from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings

# Tests get a fresh event loop per test, so pooled asyncpg connections can't be reused there.
_engine_kwargs = {"poolclass": NullPool} if settings.environment == "test" else {}
engine = create_async_engine(settings.database_url, pool_pre_ping=True, **_engine_kwargs)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
