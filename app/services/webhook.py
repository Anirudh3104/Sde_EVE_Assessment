"""Idempotent processing of payment-provider webhook events.

Everything happens in ONE transaction:

1. Lock the booking, then the payment (same lock order as the rest of the app) with
   SELECT ... FOR UPDATE, so events for the same payment are processed one at a time.
2. Record the event with INSERT ... ON CONFLICT (event_id) DO NOTHING. No row inserted means
   this event_id was already recorded: it is a DUPLICATE and nothing else is touched.
3. If the payment already reached a final status (an earlier, different event decided it), the
   new event is recorded as IGNORED. Otherwise it is APPLIED: payment and booking move to their
   new statuses through the state machine.

Even without the row locks, the unique index on event_id makes a concurrent copy of the same
event block on the insert until the first transaction commits, then see the conflict; so exactly
one delivery is ever APPLIED.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.domain.enums import BookingStatus, PaymentStatus, RefundStatus, WebhookEventResult
from app.domain.state_machine import (
    BOOKING_STATUS_FOR_PAYMENT,
    ensure_booking_transition,
    ensure_payment_transition,
    is_payment_final,
)
from app.models.booking import Booking
from app.models.payment import Payment, WebhookEvent
from app.schemas.payment import WebhookAck, WebhookEventIn

logger = structlog.get_logger(__name__)


async def process_event(
    db: AsyncSession, event: WebhookEventIn, payload: dict[str, Any]
) -> WebhookAck:
    log = logger.bind(event_id=event.event_id, payment_id=str(event.payment_id))

    booking_id = await db.scalar(select(Payment.booking_id).where(Payment.id == event.payment_id))
    if booking_id is None:
        raise NotFoundError("Payment not found", code="payment_not_found")
    booking = await db.scalar(select(Booking).where(Booking.id == booking_id).with_for_update())
    payment = await db.scalar(
        select(Payment).where(Payment.id == event.payment_id).with_for_update()
    )
    if booking is None or payment is None:  # unreachable: both FKs are ON DELETE RESTRICT
        raise NotFoundError("Payment not found", code="payment_not_found")

    new_status = PaymentStatus(event.status)
    event_row_id: uuid.UUID | None = await db.scalar(
        pg_insert(WebhookEvent)
        .values(
            event_id=event.event_id,
            payment_id=event.payment_id,
            status=new_status,
            payload=payload,
            signature_valid=True,  # unsigned/forged requests are rejected before we get here
        )
        .on_conflict_do_nothing(index_elements=["event_id"])
        .returning(WebhookEvent.id)
    )
    if event_row_id is None:
        await db.rollback()
        log.info("webhook_duplicate")
        return _ack(event, WebhookEventResult.DUPLICATE)

    if is_payment_final(payment.status):
        result = WebhookEventResult.IGNORED
        log.warning(
            "webhook_ignored_payment_final",
            payment_status=payment.status,
            event_status=new_status,
        )
    else:
        _apply(payment, booking, new_status, event.failure_reason)
        result = WebhookEventResult.APPLIED

    await db.execute(
        update(WebhookEvent)
        .where(WebhookEvent.id == event_row_id)
        .values(result=result, processed_at=datetime.now(UTC))
    )
    await db.commit()
    log.info("webhook_processed", result=result, payment_status=payment.status)
    return _ack(event, result)


def _apply(
    payment: Payment,
    booking: Booking,
    new_status: PaymentStatus,
    failure_reason: str | None,
) -> None:
    ensure_payment_transition(payment.status, new_status)
    payment.status = new_status
    if new_status == PaymentStatus.FAILED:
        payment.failure_reason = failure_reason or "Payment failed"

    target = BOOKING_STATUS_FOR_PAYMENT[new_status]
    if booking.status == BookingStatus.PENDING:
        ensure_booking_transition(booking.status, target)
        booking.status = target
        return

    # Defensive: bookings can't be cancelled while their payment is PROCESSING, so this should
    # not happen. Never resurrect a booking; if money was taken for it, flag a refund instead.
    if new_status == PaymentStatus.SUCCESS:
        payment.refund_status = RefundStatus.REFUND_PENDING
    logger.warning(
        "webhook_booking_not_pending",
        payment_id=str(payment.id),
        booking_id=str(booking.id),
        booking_status=booking.status,
        payment_status=new_status,
        refund_status=payment.refund_status,
    )


def _ack(event: WebhookEventIn, result: WebhookEventResult) -> WebhookAck:
    return WebhookAck(event_id=event.event_id, payment_id=event.payment_id, result=result)
