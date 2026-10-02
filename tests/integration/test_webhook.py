"""POST /payments/webhook/: signature auth, validation, and idempotency.

Payments are created directly in the DB (PROCESSING), so the simulated provider never runs here
unless a test drives it explicitly; webhooks are built, signed and posted by hand.
"""

import asyncio
import json
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import sign_webhook_payload
from app.domain.enums import BookingStatus, PaymentStatus, RefundStatus, WebhookEventResult
from app.main import app
from app.models import Booking
from app.models.payment import Payment, WebhookEvent
from app.services import provider_simulator
from app.services.provider_simulator import SIGNATURE_HEADER, build_event
from tests.conftest import AuthedUser, Catalog

WEBHOOK_URL = "/payments/webhook/"


@dataclass
class PendingPayment:
    booking: Booking
    payment: Payment


async def _create(
    db: AsyncSession, owner: AuthedUser, catalog: Catalog, booking_status: BookingStatus
) -> PendingPayment:
    booking = Booking(
        user_id=owner.user.id,
        centre_id=catalog.centre.id,
        test_id=catalog.test.id,
        appointment_at=(datetime.now(UTC) + timedelta(days=2)).replace(minute=0, second=0),
        amount=catalog.offering.price,
        status=booking_status,
    )
    db.add(booking)
    await db.flush()
    payment = Payment(
        booking_id=booking.id,
        user_id=owner.user.id,
        amount=booking.amount,
        status=PaymentStatus.PROCESSING,
        idempotency_key=f"key-{uuid.uuid4().hex}",
        request_hash="0" * 64,
        provider_ref=f"sim_{uuid.uuid4().hex}",
    )
    db.add(payment)
    await db.commit()
    return PendingPayment(booking=booking, payment=payment)


@pytest.fixture
async def processing(db: AsyncSession, patient: AuthedUser, catalog: Catalog) -> PendingPayment:
    return await _create(db, patient, catalog, BookingStatus.PENDING)


def encode(event: dict[str, Any]) -> bytes:
    return json.dumps(event).encode()


async def post_webhook(
    client: AsyncClient, body: bytes, signature: str | None = "auto"
) -> httpx.Response:
    headers = {"Content-Type": "application/json"}
    if signature == "auto":
        signature = sign_webhook_payload(body)
    if signature is not None:
        headers[SIGNATURE_HEADER] = signature
    return await client.post(WEBHOOK_URL, content=body, headers=headers)


async def send(client: AsyncClient, event: dict[str, Any]) -> httpx.Response:
    return await post_webhook(client, encode(event))


async def reload(db: AsyncSession, pp: PendingPayment) -> None:
    await db.refresh(pp.payment)
    await db.refresh(pp.booking)


async def event_count(db: AsyncSession) -> int:
    return await db.scalar(select(func.count()).select_from(WebhookEvent))


# --- authentication ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "signature",
    [
        None,
        "",
        "not-a-signature",
        "0" * 64,
        "SIGN_WITH_WRONG_SECRET",
    ],
)
async def test_bad_signature_is_rejected_without_side_effects(
    client: AsyncClient, db: AsyncSession, processing: PendingPayment, signature: str | None
) -> None:
    body = encode(build_event(processing.payment.id, PaymentStatus.SUCCESS))
    if signature == "SIGN_WITH_WRONG_SECRET":
        signature = sign_webhook_payload(body, secret="some-other-secret")

    resp = await post_webhook(client, body, signature=signature)

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_signature"
    await reload(db, processing)
    assert processing.payment.status == PaymentStatus.PROCESSING
    assert processing.booking.status == BookingStatus.PENDING
    assert await event_count(db) == 0


async def test_tampered_body_is_rejected(
    client: AsyncClient, db: AsyncSession, processing: PendingPayment
) -> None:
    body = encode(build_event(processing.payment.id, PaymentStatus.FAILED))
    signature = sign_webhook_payload(body)
    tampered = body.replace(b'"FAILED"', b'"SUCCESS"')

    resp = await post_webhook(client, tampered, signature=signature)

    assert resp.status_code == 401
    await reload(db, processing)
    assert processing.payment.status == PaymentStatus.PROCESSING


# --- validation -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e.pop("event_id"),
        lambda e: e.update(event_id=""),
        lambda e: e.update(event_id="x" * 65),
        lambda e: e.update(payment_id="not-a-uuid"),
        lambda e: e.update(status="PROCESSING"),
        lambda e: e.update(occurred_at="2026-01-01T10:00:00"),  # naive datetime
    ],
    ids=["no-event-id", "empty-event-id", "long-event-id", "bad-uuid", "bad-status", "naive-ts"],
)
async def test_malformed_payload_is_rejected(
    client: AsyncClient, db: AsyncSession, processing: PendingPayment, mutate
) -> None:
    event = build_event(processing.payment.id, PaymentStatus.SUCCESS)
    mutate(event)

    resp = await send(client, event)

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"
    assert await event_count(db) == 0


async def test_invalid_json_is_rejected(client: AsyncClient, processing: PendingPayment) -> None:
    resp = await post_webhook(client, b"{not json")
    assert resp.status_code == 422


async def test_unknown_payment_is_not_found(
    client: AsyncClient, db: AsyncSession, processing: PendingPayment
) -> None:
    resp = await send(client, build_event(uuid.uuid4(), PaymentStatus.SUCCESS))

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "payment_not_found"
    assert await event_count(db) == 0


# --- applying events --------------------------------------------------------------------------


async def test_success_event_confirms_booking(
    client: AsyncClient, db: AsyncSession, processing: PendingPayment
) -> None:
    event = build_event(processing.payment.id, PaymentStatus.SUCCESS)

    resp = await send(client, event)

    assert resp.status_code == 200
    assert resp.json() == {
        "event_id": event["event_id"],
        "payment_id": str(processing.payment.id),
        "result": "APPLIED",
    }
    await reload(db, processing)
    assert processing.payment.status == PaymentStatus.SUCCESS
    assert processing.booking.status == BookingStatus.CONFIRMED
    row = await db.scalar(select(WebhookEvent))
    assert row.event_id == event["event_id"]
    assert row.result == WebhookEventResult.APPLIED
    assert row.signature_valid is True
    assert row.processed_at is not None
    assert row.payload == event


async def test_failed_event_fails_booking(
    client: AsyncClient, db: AsyncSession, processing: PendingPayment
) -> None:
    event = build_event(processing.payment.id, PaymentStatus.FAILED, "card_declined")

    resp = await send(client, event)

    assert resp.json()["result"] == "APPLIED"
    await reload(db, processing)
    assert processing.payment.status == PaymentStatus.FAILED
    assert processing.payment.failure_reason == "card_declined"
    assert processing.booking.status == BookingStatus.FAILED


# --- idempotency ------------------------------------------------------------------------------


async def test_redelivered_event_is_a_duplicate_noop(
    client: AsyncClient, db: AsyncSession, processing: PendingPayment
) -> None:
    body = encode(build_event(processing.payment.id, PaymentStatus.SUCCESS))

    first = await post_webhook(client, body)
    await reload(db, processing)
    updated_at = processing.payment.updated_at
    second = await post_webhook(client, body)

    assert first.json()["result"] == "APPLIED"
    assert second.status_code == 200  # 2xx so the provider stops retrying
    assert second.json()["result"] == "DUPLICATE"
    assert await event_count(db) == 1
    await reload(db, processing)
    assert processing.payment.status == PaymentStatus.SUCCESS
    assert processing.payment.updated_at == updated_at
    assert processing.booking.status == BookingStatus.CONFIRMED


async def test_reused_event_id_with_different_content_is_still_a_duplicate(
    client: AsyncClient, db: AsyncSession, processing: PendingPayment
) -> None:
    event = build_event(processing.payment.id, PaymentStatus.SUCCESS)
    await send(client, event)

    resp = await send(client, {**event, "status": "FAILED", "failure_reason": "late"})

    assert resp.json()["result"] == "DUPLICATE"
    await reload(db, processing)
    assert processing.payment.status == PaymentStatus.SUCCESS
    assert processing.booking.status == BookingStatus.CONFIRMED


async def test_conflicting_event_after_final_state_is_ignored(
    client: AsyncClient, db: AsyncSession, processing: PendingPayment
) -> None:
    await send(client, build_event(processing.payment.id, PaymentStatus.SUCCESS))
    late = build_event(processing.payment.id, PaymentStatus.FAILED, "late failure")

    resp = await send(client, late)

    assert resp.status_code == 200
    assert resp.json()["result"] == "IGNORED"
    await reload(db, processing)
    assert processing.payment.status == PaymentStatus.SUCCESS
    assert processing.payment.failure_reason is None
    assert processing.booking.status == BookingStatus.CONFIRMED
    ignored = await db.scalar(select(WebhookEvent).where(WebhookEvent.event_id == late["event_id"]))
    assert ignored.result == WebhookEventResult.IGNORED  # recorded for audit, but not applied


async def test_concurrent_duplicate_deliveries_apply_exactly_once(
    client: AsyncClient, db: AsyncSession, processing: PendingPayment
) -> None:
    body = encode(build_event(processing.payment.id, PaymentStatus.SUCCESS))

    responses = await asyncio.gather(*(post_webhook(client, body) for _ in range(5)))

    assert all(r.status_code == 200 for r in responses)
    assert Counter(r.json()["result"] for r in responses) == {"APPLIED": 1, "DUPLICATE": 4}
    assert await event_count(db) == 1
    await reload(db, processing)
    assert processing.payment.status == PaymentStatus.SUCCESS
    assert processing.booking.status == BookingStatus.CONFIRMED


async def test_concurrent_conflicting_events_leave_consistent_state(
    client: AsyncClient, db: AsyncSession, processing: PendingPayment
) -> None:
    success = build_event(processing.payment.id, PaymentStatus.SUCCESS)
    failure = build_event(processing.payment.id, PaymentStatus.FAILED, "declined")

    responses = await asyncio.gather(send(client, success), send(client, failure))

    results = {r.json()["event_id"]: r.json()["result"] for r in responses}
    assert sorted(results.values()) == ["APPLIED", "IGNORED"]
    winner = success if results[success["event_id"]] == "APPLIED" else failure
    await reload(db, processing)
    assert processing.payment.status == winner["status"]
    expected = BookingStatus.CONFIRMED if winner is success else BookingStatus.FAILED
    assert processing.booking.status == expected


async def test_success_for_cancelled_booking_flags_refund(
    client: AsyncClient, db: AsyncSession, patient: AuthedUser, catalog: Catalog
) -> None:
    # Defensive path: cancellation is blocked while a payment is PROCESSING, but if a booking is
    # somehow cancelled anyway, a late SUCCESS must not resurrect it.
    pp = await _create(db, patient, catalog, BookingStatus.CANCELLED)

    resp = await send(client, build_event(pp.payment.id, PaymentStatus.SUCCESS))

    assert resp.json()["result"] == "APPLIED"
    await reload(db, pp)
    assert pp.booking.status == BookingStatus.CANCELLED
    assert pp.payment.status == PaymentStatus.SUCCESS
    assert pp.payment.refund_status == RefundStatus.REFUND_PENDING


# --- simulated provider end-to-end ------------------------------------------------------------


@pytest.fixture
def provider_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provider_simulator, "retry_base_delay_seconds", 0)
    monkeypatch.setattr(settings, "webhook_target_url", "http://test/payments/webhook/")
    monkeypatch.setattr(settings, "provider_max_attempts", 3)


async def test_provider_retries_transient_failures_with_same_event(
    db: AsyncSession,
    processing: PendingPayment,
    monkeypatch: pytest.MonkeyPatch,
    provider_settings: None,
) -> None:
    seen: list[bytes] = []
    real_app = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    async def flaky(request: httpx.Request) -> httpx.Response:
        seen.append(request.content)
        if len(seen) == 1:
            raise httpx.ConnectError("connection refused", request=request)
        if len(seen) == 2:
            return httpx.Response(503)
        forwarded = await real_app.post(
            request.url.path, content=request.content, headers=dict(request.headers)
        )
        return httpx.Response(forwarded.status_code, content=forwarded.content)

    monkeypatch.setattr(
        provider_simulator,
        "client_factory",
        lambda: AsyncClient(transport=httpx.MockTransport(flaky)),
    )
    event = build_event(processing.payment.id, PaymentStatus.SUCCESS)

    async with real_app:
        delivered = await provider_simulator.deliver(event)

    assert delivered is True
    assert len(seen) == 3
    assert len(set(seen)) == 1  # every retry carried the identical, identically-signed body
    assert json.loads(seen[0])["event_id"] == event["event_id"]
    await reload(db, processing)
    assert processing.payment.status == PaymentStatus.SUCCESS
    assert processing.booking.status == BookingStatus.CONFIRMED
    assert await event_count(db) == 1


async def test_provider_duplicate_delivery_is_deduplicated(
    db: AsyncSession,
    processing: PendingPayment,
    monkeypatch: pytest.MonkeyPatch,
    provider_settings: None,
) -> None:
    results: list[str] = []

    async def record(response: httpx.Response) -> None:
        await response.aread()
        results.append(response.json()["result"])

    monkeypatch.setattr(settings, "simulate_duplicate_webhooks", True)
    monkeypatch.setattr(
        provider_simulator,
        "client_factory",
        lambda: AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            event_hooks={"response": [record]},
        ),
    )

    delivered = await provider_simulator.deliver(
        build_event(processing.payment.id, PaymentStatus.FAILED, "declined")
    )

    assert delivered is True
    assert results == ["APPLIED", "DUPLICATE"]
    assert await event_count(db) == 1
    await reload(db, processing)
    assert processing.payment.status == PaymentStatus.FAILED
    assert processing.booking.status == BookingStatus.FAILED
