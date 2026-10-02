"""Rate limiting is switched off for the rest of the suite; this test turns it back on."""

import pytest
from httpx import AsyncClient

from app.core.config import settings
from app.core.rate_limit import limiter


@pytest.fixture
def rate_limiting_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(limiter, "enabled", True)
    monkeypatch.setattr(settings, "login_rate_limit", "3/minute")
    yield


async def test_login_is_rate_limited(client: AsyncClient, rate_limiting_on) -> None:
    body = {"email": "nobody@example.com", "password": "wrong-password"}

    for _ in range(3):
        assert (await client.post("/auth/login", json=body)).status_code == 401

    response = await client.post("/auth/login", json=body)

    assert response.status_code == 429
    error = response.json()["error"]
    assert error["code"] == "rate_limited"
    assert error["request_id"]
