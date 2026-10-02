"""Booking use cases: create, list, read and cancel.

Every function owns its transaction (commits on success) and raises `AppError` subclasses for
business-rule failures, so routers stay a thin HTTP layer.
"""

import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.domain.enums import BookingStatus, PaymentStatus, RefundStatus, UserRole
from app.domain.state_machine import ensure_booking_transition
from app.models import Booking, Centre, CentreTest, DiagnosticTest, Payment, User
from app.schemas.booking import BookingCreate

logger = structlog.get_logger(__name__)

# How far ahead a slot may be booked.
MAX_BOOKING_HORIZON = timedelta(days=90)
_ACTIVE_SLOT_INDEX = "uq_bookings_active_slot"


def _with_details() -> Select[tuple[Booking]]:
    """Bookings with centre and test eagerly loaded (async sessions cannot lazy-load)."""
    return select(Booking).options(joinedload(Booking.centre), joinedload(Booking.test))


async def _reload(db: AsyncSession, booking_id: uuid.UUID) -> Booking:
    """Re-read a booking after commit so server-generated columns (timestamps) are fresh."""
    stmt = _with_details().where(Booking.id == booking_id).execution_options(populate_existing=True)
    return (await db.scalars(stmt)).one()


async def create_booking(db: AsyncSession, user: User, data: BookingCreate) -> Booking:
    now = datetime.now(UTC)
    if data.appointment_at <= now:
        raise ValidationError("Appointment time must be in the future", code="appointment_in_past")
    if data.appointment_at > now + MAX_BOOKING_HORIZON:
        raise ValidationError(
            f"Appointments can be booked at most {MAX_BOOKING_HORIZON.days} days ahead",
            code="appointment_too_far",
        )

    centre = await db.get(Centre, data.centre_id)
    if centre is None or not centre.is_active:
        raise NotFoundError("Centre not found")

    offering = await db.scalar(
        select(CentreTest)
        .join(CentreTest.test)
        .where(
            CentreTest.centre_id == data.centre_id,
            CentreTest.test_id == data.test_id,
            CentreTest.is_available.is_(True),
            DiagnosticTest.is_active.is_(True),
        )
    )
    if offering is None:
        raise ValidationError("Test not offered at this centre", code="test_not_offered")

    booking = Booking(
        user_id=user.id,
        centre_id=data.centre_id,
        test_id=data.test_id,
        appointment_at=data.appointment_at,
        amount=offering.price,  # snapshot of the server-side price
        status=BookingStatus.PENDING,
    )
    db.add(booking)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if _ACTIVE_SLOT_INDEX in str(exc.orig):
            raise ConflictError(
                "You already have an active booking for this test, centre and time",
                code="duplicate_booking",
            ) from exc
        raise

    logger.info(
        "booking_created",
        booking_id=str(booking.id),
        user_id=str(user.id),
        centre_id=str(booking.centre_id),
        test_id=str(booking.test_id),
        amount=str(booking.amount),
    )
    return await _reload(db, booking.id)


async def list_bookings(
    db: AsyncSession,
    user: User,
    *,
    status: BookingStatus | None,
    limit: int,
    offset: int,
) -> tuple[list[Booking], int]:
    """Newest first. Patients see their own bookings; admins see everyone's."""
    filters = []
    if user.role != UserRole.ADMIN:
        filters.append(Booking.user_id == user.id)
    if status is not None:
        filters.append(Booking.status == status)

    total = await db.scalar(select(func.count()).select_from(Booking).where(*filters))
    rows = await db.scalars(
        _with_details()
        .where(*filters)
        .order_by(Booking.created_at.desc(), Booking.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(rows), total or 0


async def get_booking_for_user(db: AsyncSession, booking_id: uuid.UUID, user: User) -> Booking:
    """Fetch a booking visible to `user` (its owner, or any admin).

    Anything else is reported as 404 rather than 403 so ids of other users' bookings leak nothing.
    """
    stmt = _with_details().where(Booking.id == booking_id)
    if user.role != UserRole.ADMIN:
        stmt = stmt.where(Booking.user_id == user.id)
    booking = await db.scalar(stmt)
    if booking is None:
        raise NotFoundError("Booking not found")
    return booking


async def cancel_booking(db: AsyncSession, booking_id: uuid.UUID, user: User) -> Booking:
    """Cancel the caller's own booking.

    The booking row (then its payment) is locked FOR UPDATE so a concurrent payment webhook
    cannot confirm the booking between our checks and our write.
    """
    booking = await db.scalar(
        select(Booking)
        .where(Booking.id == booking_id, Booking.user_id == user.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if booking is None:
        raise NotFoundError("Booking not found")

    previous = booking.status
    ensure_booking_transition(previous, BookingStatus.CANCELLED)

    payment = await db.scalar(
        select(Payment).where(Payment.booking_id == booking.id).with_for_update()
    )
    if payment is not None and payment.status == PaymentStatus.PROCESSING:
        raise ConflictError(
            "A payment for this booking is in progress; try again once it completes",
            code="payment_in_progress",
        )

    now = datetime.now(UTC)
    if booking.appointment_at <= now:
        raise ConflictError(
            "The appointment time has passed; it can no longer be cancelled",
            code="appointment_passed",
        )

    booking.status = BookingStatus.CANCELLED
    booking.cancelled_at = now
    # Refunds are simulated: we only record that one is owed.
    refund_requested = False
    if payment is not None and payment.status == PaymentStatus.SUCCESS:
        payment.refund_status = RefundStatus.REFUND_PENDING
        refund_requested = True
    await db.commit()

    logger.info(
        "booking_status_changed",
        booking_id=str(booking.id),
        user_id=str(user.id),
        from_status=previous,
        to_status=BookingStatus.CANCELLED,
        refund_requested=refund_requested,
    )
    return await _reload(db, booking.id)
