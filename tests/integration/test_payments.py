"""POST /payments/ and GET /payments/{id}.

The simulated provider is wired to deliver its webhook in-process (through the ASGI app), so a
payment's whole lifecycle runs for real: API -> background task -> signed webhook -> DB. httpx's
ASGITransport only returns once the app (including its background tasks) has finished, so the
outcome is already applied when `client.post` returns.
"""

import asyncio
import itertools
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.domain.enums import BookingStatus, PaymentStatus, WebhookEventResult
from app.main import app
from app.models import Booking
from app.models.payment import Payment, WebhookEvent
from app.services import provider_simulator
from tests.conftest import AuthedUser, Catalog

BookingFactory = Callable[..., Awaitable[Booking]]


@pytest.fixture(autouse=True)
def in_process_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    def client_factory() -> AsyncClient:
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    monkeypatch.setattr(provider_simulator, "client_factory", client_factory)
    monkeypatch.setattr(provider_simulator, "retry_base_delay_seconds", 0)
    monkeypatch.setattr(settings, "webhook_target_url", "http://test/payments/webhook/")


@pytest.fixture
def make_booking(db: AsyncSession, catalog: Catalog) -> BookingFactory:
    days = itertools.count(2)  # distinct slots, so the active-slot unique index never trips

    async def _make(
        owner: AuthedUser,
        status: BookingStatus = BookingStatus.PENDING,
        appointment_at: datetime | None = None,
    ) -> Booking:
        at = appointment_at or (datetime.now(UTC) + timedelta(days=next(days))).replace(
            minute=0, second=0, microsecond=0
        )
        booking = Booking(
            user_id=owner.user.id,
            centre_id=catalog.centre.id,
            test_id=catalog.test.id,
            appointment_at=at,
            amount=catalog.offering.price,
            status=status,
        )
        db.add(booking)
        await db.commit()
        return booking

    return _make


def idem(key: str | None = None) -> dict[str, str]:
    return {"Idempotency-Key": key or f"key-{uuid.uuid4().hex}"}


async def count(db: AsyncSession, model: type) -> int:
    return await db.scalar(select(func.count()).select_from(model))


async def pay(
    client: AsyncClient,
    user: AuthedUser,
    booking_id: uuid.UUID,
    *,
    key: str | None = None,
    outcome: str | None = "success",
):
    return await client.post(
        "/payments/",
        json={"booking_id": str(booking_id), "simulate_outcome": outcome},
        headers={**user.headers, **idem(key)},
    )


# --- request validation & auth ---------------------------------------------------------------


async def test_requires_authentication(client: AsyncClient, make_booking, patient) -> None:
    booking = await make_booking(patient)
    resp = await client.post("/payments/", json={"booking_id": str(booking.id)}, headers=idem())
    assert resp.status_code == 401


async def test_missing_idempotency_key_is_rejected(
    client: AsyncClient, db: AsyncSession, make_booking, patient
) -> None:
    booking = await make_booking(patient)
    resp = await client.post(
        "/payments/", json={"booking_id": str(booking.id)}, headers=patient.headers
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"
    assert await count(db, Payment) == 0


async def test_overlong_idempotency_key_is_rejected(client, make_booking, patient) -> None:
    booking = await make_booking(patient)
    resp = await pay(client, patient, booking.id, key="k" * 256)
    assert resp.status_code == 422


async def test_unknown_body_fields_are_rejected(client, make_booking, patient) -> None:
    booking = await make_booking(patient)
    resp = await client.post(
        "/payments/",
        json={"booking_id": str(booking.id), "amount": "1.00"},
        headers={**patient.headers, **idem()},
    )
    assert resp.status_code == 422


# --- happy paths ------------------------------------------------------------------------------


async def test_successful_payment_confirms_booking(
    client: AsyncClient, db: AsyncSession, make_booking, patient
) -> None:
    booking = await make_booking(patient)

    resp = await pay(client, patient, booking.id, outcome="success")

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "PROCESSING"  # the outcome arrives later, via the webhook
    assert body["booking_id"] == str(booking.id)
    assert Decimal(body["amount"]) == booking.amount == Decimal("499.00")
    assert body["currency"] == "INR"
    assert body["provider_ref"].startswith("sim_")
    assert body["failure_reason"] is None

    # The provider has delivered its webhook by now (see module docstring).
    got = await client.get(f"/payments/{body['id']}", headers=patient.headers)
    assert got.json()["status"] == "SUCCESS"
    await db.refresh(booking)
    assert booking.status == BookingStatus.CONFIRMED
    event = await db.scalar(select(WebhookEvent))
    assert event.result == WebhookEventResult.APPLIED
    assert str(event.payment_id) == body["id"]


async def test_failed_payment_fails_booking(
    client: AsyncClient, db: AsyncSession, make_booking, patient
) -> None:
    booking = await make_booking(patient)

    resp = await pay(client, patient, booking.id, outcome="failure")

    assert resp.status_code == 202
    payment = await db.get(Payment, uuid.UUID(resp.json()["id"]))
    assert payment.status == PaymentStatus.FAILED
    assert payment.failure_reason
    await db.refresh(booking)
    assert booking.status == BookingStatus.FAILED


# --- idempotency ------------------------------------------------------------------------------


async def test_replay_with_same_key_returns_original_payment(
    client: AsyncClient, db: AsyncSession, make_booking, patient
) -> None:
    booking = await make_booking(patient)

    first = await pay(client, patient, booking.id, key="order-1")
    second = await pay(client, patient, booking.id, key="order-1")

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.headers["Idempotent-Replayed"] == "true"
    assert second.json()["id"] == first.json()["id"]
    assert await count(db, Payment) == 1
    assert await count(db, WebhookEvent) == 1  # the provider was not triggered again


async def test_same_key_with_different_body_is_rejected(
    client: AsyncClient, db: AsyncSession, make_booking, patient
) -> None:
    booking = await make_booking(patient)
    await pay(client, patient, booking.id, key="order-1", outcome="success")

    resp = await pay(client, patient, booking.id, key="order-1", outcome="failure")

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "idempotency_key_reuse"
    assert await count(db, Payment) == 1


async def test_idempotency_keys_are_scoped_per_user(
    client: AsyncClient, db: AsyncSession, make_booking, patient, other_patient
) -> None:
    mine = await make_booking(patient)
    theirs = await make_booking(other_patient)

    assert (await pay(client, patient, mine.id, key="shared")).status_code == 202
    assert (await pay(client, other_patient, theirs.id, key="shared")).status_code == 202
    assert await count(db, Payment) == 2


async def test_concurrent_requests_with_same_key_create_one_payment(
    client: AsyncClient, db: AsyncSession, make_booking, patient
) -> None:
    booking = await make_booking(patient)

    responses = await asyncio.gather(
        *(pay(client, patient, booking.id, key="order-1") for _ in range(3))
    )

    assert sorted(r.status_code for r in responses) == [200, 200, 202]
    assert len({r.json()["id"] for r in responses}) == 1
    assert await count(db, Payment) == 1


# --- business rules ---------------------------------------------------------------------------


async def test_second_payment_for_same_booking_conflicts(
    client: AsyncClient, db: AsyncSession, make_booking, patient
) -> None:
    booking = await make_booking(patient)
    await pay(client, patient, booking.id, outcome="failure")

    resp = await pay(client, patient, booking.id)  # new Idempotency-Key

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "payment_already_exists"
    assert await count(db, Payment) == 1


async def test_cannot_pay_for_another_users_booking(
    client: AsyncClient, db: AsyncSession, make_booking, patient, other_patient
) -> None:
    booking = await make_booking(other_patient)

    resp = await pay(client, patient, booking.id)

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "booking_not_found"
    assert await count(db, Payment) == 0


async def test_unknown_booking_is_not_found(client: AsyncClient, patient, catalog) -> None:
    resp = await pay(client, patient, uuid.uuid4())
    assert resp.status_code == 404


@pytest.mark.parametrize(
    "status", [BookingStatus.CANCELLED, BookingStatus.FAILED, BookingStatus.CONFIRMED]
)
async def test_only_pending_bookings_can_be_paid(
    client: AsyncClient, db: AsyncSession, make_booking, patient, status: BookingStatus
) -> None:
    booking = await make_booking(patient, status=status)

    resp = await pay(client, patient, booking.id)

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "booking_not_payable"
    assert await count(db, Payment) == 0


async def test_cannot_pay_after_appointment_time(
    client: AsyncClient, db: AsyncSession, make_booking, patient
) -> None:
    booking = await make_booking(patient, appointment_at=datetime.now(UTC) - timedelta(hours=1))

    resp = await pay(client, patient, booking.id)

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "appointment_passed"
    assert await count(db, Payment) == 0


# --- GET /payments/{id} -----------------------------------------------------------------------


async def test_get_payment_visible_to_owner_and_admin_only(
    client: AsyncClient, make_booking, patient, other_patient, admin
) -> None:
    booking = await make_booking(patient)
    payment_id = (await pay(client, patient, booking.id)).json()["id"]

    owner = await client.get(f"/payments/{payment_id}", headers=patient.headers)
    by_admin = await client.get(f"/payments/{payment_id}", headers=admin.headers)
    stranger = await client.get(f"/payments/{payment_id}", headers=other_patient.headers)

    assert owner.status_code == 200
    assert owner.json()["id"] == payment_id
    assert by_admin.status_code == 200
    assert stranger.status_code == 404


async def test_get_unknown_payment_is_not_found(client: AsyncClient, patient) -> None:
    resp = await client.get(f"/payments/{uuid.uuid4()}", headers=patient.headers)
    assert resp.status_code == 404
