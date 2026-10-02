"""Payment initiation and lookup.

POST /payments/ is idempotent per (user, Idempotency-Key): replaying the same key with the same
body returns the original payment; reusing a key with a different body is rejected. Separately,
a booking can only ever have one payment (UNIQUE payments.booking_id).

Lock ordering: the booking row is always locked before its payment row (here and in the webhook
service) so concurrent payment/webhook/cancel flows cannot deadlock.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.domain.enums import BookingStatus, PaymentStatus, UserRole
from app.models.booking import Booking
from app.models.payment import Payment
from app.models.user import User
from app.schemas.payment import PaymentCreate

logger = structlog.get_logger(__name__)

IDEMPOTENCY_CONSTRAINT = "uq_payments_user_idempotency_key"


@dataclass(frozen=True)
class PaymentResult:
    payment: Payment
    replayed: bool  # True when an earlier request with the same Idempotency-Key is replayed


def hash_request(data: PaymentCreate) -> str:
    """SHA-256 of the canonical JSON form of the request body."""
    canonical = json.dumps(data.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


async def create_payment(
    db: AsyncSession, user: User, data: PaymentCreate, idempotency_key: str
) -> PaymentResult:
    # Captured up front: a rollback expires ORM objects, and async sessions can't lazy-reload.
    user_id = user.id
    request_hash = hash_request(data)

    existing = await _get_by_idempotency_key(db, user_id, idempotency_key)
    if existing is not None:
        return _replay(existing, request_hash)

    booking = await db.scalar(
        select(Booking).where(Booking.id == data.booking_id).with_for_update()
    )
    if booking is None or booking.user_id != user_id:
        raise NotFoundError("Booking not found", code="booking_not_found")

    # Checked while holding the booking lock, so a concurrent request for this booking has either
    # committed its payment (and we see it) or is waiting behind us.
    existing = await db.scalar(select(Payment).where(Payment.booking_id == booking.id))
    if existing is not None:
        if existing.user_id == user_id and existing.idempotency_key == idempotency_key:
            return _replay(existing, request_hash)
        raise ConflictError(
            "A payment already exists for this booking",
            code="payment_already_exists",
            details={"payment_id": str(existing.id), "status": existing.status},
        )
    if booking.status != BookingStatus.PENDING:
        raise ConflictError(
            f"Only PENDING bookings can be paid for (booking is {booking.status})",
            code="booking_not_payable",
            details={"booking_status": booking.status},
        )
    if booking.appointment_at <= datetime.now(UTC):
        raise ConflictError("The appointment time has already passed", code="appointment_passed")

    payment = Payment(
        booking_id=booking.id,
        user_id=user_id,
        amount=booking.amount,
        currency=booking.currency,
        status=PaymentStatus.PROCESSING,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        provider_ref=f"sim_{uuid.uuid4().hex}",
    )
    db.add(payment)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if IDEMPOTENCY_CONSTRAINT in str(exc.orig):
            # A concurrent request with the same key (for a different booking) won the race.
            winner = await _get_by_idempotency_key(db, user_id, idempotency_key)
            if winner is not None:
                return _replay(winner, request_hash)
        raise ConflictError(
            "A payment already exists for this booking", code="payment_already_exists"
        ) from exc

    await db.refresh(payment)  # load server-generated timestamps
    logger.info(
        "payment_created",
        payment_id=str(payment.id),
        booking_id=str(payment.booking_id),
        amount=str(payment.amount),
    )
    return PaymentResult(payment=payment, replayed=False)


async def get_payment(db: AsyncSession, user: User, payment_id: uuid.UUID) -> Payment:
    payment = await db.get(Payment, payment_id)
    # 404 (not 403) for other users' payments, so ids can't be probed.
    if payment is None or (user.role != UserRole.ADMIN and payment.user_id != user.id):
        raise NotFoundError("Payment not found", code="payment_not_found")
    return payment


async def _get_by_idempotency_key(
    db: AsyncSession, user_id: uuid.UUID, idempotency_key: str
) -> Payment | None:
    return await db.scalar(
        select(Payment).where(
            Payment.user_id == user_id, Payment.idempotency_key == idempotency_key
        )
    )


def _replay(payment: Payment, request_hash: str) -> PaymentResult:
    if payment.request_hash != request_hash:
        raise ValidationError(
            "Idempotency-Key was already used with a different request body",
            code="idempotency_key_reuse",
        )
    logger.info("payment_idempotent_replay", payment_id=str(payment.id))
    return PaymentResult(payment=payment, replayed=True)
