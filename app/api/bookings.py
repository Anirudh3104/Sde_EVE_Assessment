import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.core.deps import CurrentUser, DbSession
from app.domain.enums import BookingStatus
from app.schemas.booking import BookingCreate, BookingOut
from app.schemas.common import ErrorResponse, Page, Pagination
from app.services import booking as booking_service

router = APIRouter(prefix="/bookings", tags=["bookings"])

_NOT_FOUND = {404: {"model": ErrorResponse, "description": "Booking not found"}}


@router.post(
    "/",
    response_model=BookingOut,
    status_code=status.HTTP_201_CREATED,
    summary="Book a diagnostic test",
    responses={
        404: {"model": ErrorResponse, "description": "Centre not found or inactive"},
        409: {"model": ErrorResponse, "description": "Duplicate active booking for this slot"},
        422: {"model": ErrorResponse, "description": "Invalid time or test not offered"},
    },
)
async def create_booking(body: BookingCreate, db: DbSession, user: CurrentUser) -> BookingOut:
    """Create a PENDING booking for the caller.

    The amount is copied from the centre's current price for the test; clients never send it.
    `appointment_at` must include a UTC offset and lie within the next 90 days.
    """
    booking = await booking_service.create_booking(db, user, body)
    return BookingOut.model_validate(booking)


@router.get("/", response_model=Page[BookingOut], summary="List bookings")
async def list_bookings(
    db: DbSession,
    user: CurrentUser,
    page: Pagination,
    status_filter: Annotated[
        BookingStatus | None, Query(alias="status", description="Filter by status")
    ] = None,
) -> Page[BookingOut]:
    """Newest first. Patients see only their own bookings; admins see all bookings."""
    items, total = await booking_service.list_bookings(
        db, user, status=status_filter, limit=page.limit, offset=page.offset
    )
    return Page[BookingOut](
        items=[BookingOut.model_validate(b) for b in items],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get(
    "/{booking_id}", response_model=BookingOut, summary="Get a booking", responses=_NOT_FOUND
)
async def get_booking(booking_id: uuid.UUID, db: DbSession, user: CurrentUser) -> BookingOut:
    """Visible to the booking's owner and to admins; anyone else gets 404."""
    booking = await booking_service.get_booking_for_user(db, booking_id, user)
    return BookingOut.model_validate(booking)


@router.post(
    "/{booking_id}/cancel",
    response_model=BookingOut,
    summary="Cancel a booking",
    responses={
        **_NOT_FOUND,
        409: {
            "model": ErrorResponse,
            "description": "Already final, payment in progress, or appointment already passed",
        },
    },
)
async def cancel_booking(booking_id: uuid.UUID, db: DbSession, user: CurrentUser) -> BookingOut:
    """Cancel one of the caller's own PENDING or CONFIRMED bookings.

    Not allowed while a payment is PROCESSING or once the appointment time has passed.
    Cancelling a paid (CONFIRMED) booking marks its payment `REFUND_PENDING` (simulated refund).
    """
    booking = await booking_service.cancel_booking(db, booking_id, user)
    return BookingOut.model_validate(booking)
