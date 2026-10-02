"""Shared test fixtures.

Tests run against a real PostgreSQL database (TEST_DATABASE_URL, default `eve_test` on the
compose `db` service) because row locks, partial unique indexes and ON CONFLICT are part of
what we are testing. The schema is created by running the real Alembic migrations once per
session; every test then starts from empty tables.
"""

import os

# Must happen before any `app` import: settings and the engine are created at import time.
os.environ["ENVIRONMENT"] = "test"
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://eve:eve@localhost:5432/eve_test"
)
os.environ["REDIS_URL"] = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/1")
os.environ["RATE_LIMIT_ENABLED"] = "false"
os.environ["PROVIDER_DELAY_SECONDS"] = "0"
os.environ["SIMULATE_DUPLICATE_WEBHOOKS"] = "false"
os.environ["LOG_JSON"] = "false"

import uuid  # noqa: E402
from collections.abc import AsyncIterator, Awaitable, Callable  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402
from decimal import Decimal  # noqa: E402

import asyncpg  # noqa: E402
import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.security import create_access_token, hash_password  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.domain.enums import UserRole  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Centre, CentreTest, DiagnosticTest, User  # noqa: E402

TABLES = "webhook_events, payments, bookings, centre_tests, diagnostic_tests, centres, users"
TEST_PASSWORD = "Str0ngPassw0rd!"


async def _ensure_test_database() -> None:
    url = make_url(settings.database_url)
    conn = await asyncpg.connect(
        user=url.username,
        password=url.password,
        host=url.host,
        port=url.port or 5432,
        database="postgres",
    )
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", url.database)
        if not exists:
            await conn.execute(f'CREATE DATABASE "{url.database}"')
    finally:
        await conn.close()


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


# --- users & auth --------------------------------------------------------------------------


@dataclass
class AuthedUser:
    user: User
    token: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


UserFactory = Callable[..., Awaitable[AuthedUser]]


@pytest.fixture
def make_user(db: AsyncSession) -> UserFactory:
    async def _make(role: UserRole = UserRole.PATIENT, email: str | None = None) -> AuthedUser:
        user = User(
            email=email or f"user-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password(TEST_PASSWORD),
            full_name="Test User",
            role=role,
        )
        db.add(user)
        await db.commit()
        token, _ = create_access_token(user.id, user.role)
        return AuthedUser(user=user, token=token)

    return _make


@pytest.fixture
async def patient(make_user: UserFactory) -> AuthedUser:
    return await make_user()


@pytest.fixture
async def other_patient(make_user: UserFactory) -> AuthedUser:
    return await make_user()


@pytest.fixture
async def admin(make_user: UserFactory) -> AuthedUser:
    return await make_user(role=UserRole.ADMIN)


# --- catalogue ------------------------------------------------------------------------------


@dataclass
class Catalog:
    centre: Centre
    test: DiagnosticTest
    offering: CentreTest
    unavailable_test: DiagnosticTest  # offered by the centre but is_available=False
    unoffered_test: DiagnosticTest  # not offered by the centre at all


@pytest.fixture
async def catalog(db: AsyncSession) -> Catalog:
    centre = Centre(
        name="Apollo Diagnostics", address="1 MG Road", city="Bengaluru", pincode="560001"
    )
    cbc = DiagnosticTest(code="CBC", name="Complete Blood Count")
    lipid = DiagnosticTest(code="LIPID", name="Lipid Profile")
    tsh = DiagnosticTest(code="TSH", name="Thyroid Stimulating Hormone")
    db.add_all([centre, cbc, lipid, tsh])
    await db.flush()
    offering = CentreTest(centre_id=centre.id, test_id=cbc.id, price=Decimal("499.00"))
    unavailable = CentreTest(
        centre_id=centre.id, test_id=lipid.id, price=Decimal("899.00"), is_available=False
    )
    db.add_all([offering, unavailable])
    await db.commit()
    return Catalog(
        centre=centre, test=cbc, offering=offering, unavailable_test=lipid, unoffered_test=tsh
    )


def future_slot(days: int = 2) -> str:
    """An ISO-8601 appointment time in the future, on the hour."""
    at = (datetime.now(UTC) + timedelta(days=days)).replace(minute=0, second=0, microsecond=0)
    return at.isoformat()
