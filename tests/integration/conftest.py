"""Integration-test setup: migrate the real Postgres schema once, start every test clean.

Kept out of the root conftest so unit tests (pure logic) run without any database.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.core.config import settings
from app.core.redis import close_redis, get_redis
from app.db.session import SessionLocal
from tests.conftest import TABLES, _ensure_test_database


@pytest.fixture(scope="session", autouse=True)
def _migrated_database() -> None:
    asyncio.run(_ensure_test_database())
    cfg = Config("alembic.ini")
    cfg.cmd_opts = type("Opts", (), {"x": [f"db_url={settings.database_url}"]})()
    command.upgrade(cfg, "head")


@pytest.fixture(autouse=True)
async def _clean_state() -> AsyncIterator[None]:
    async with SessionLocal() as session:
        await session.execute(text(f"TRUNCATE {TABLES} CASCADE"))
        await session.commit()
    with contextlib.suppress(Exception):  # Redis is optional; the cache fails open.
        await get_redis().flushdb()
    yield
    await close_redis()  # the client is bound to this test's event loop
