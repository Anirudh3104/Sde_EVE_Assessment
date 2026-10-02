"""Pure transition rules for bookings and payments.

Services call `ensure_*_transition` before mutating a status, so every state change in the
system goes through one place and illegal moves surface as 409 `invalid_state_transition`.
"""

from app.core.errors import InvalidStateTransition
from app.domain.enums import BookingStatus, PaymentStatus

BOOKING_TRANSITIONS: dict[BookingStatus, frozenset[BookingStatus]] = {
    BookingStatus.PENDING: frozenset(
        {BookingStatus.CONFIRMED, BookingStatus.FAILED, BookingStatus.CANCELLED}
    ),
    BookingStatus.CONFIRMED: frozenset({BookingStatus.CANCELLED}),
    BookingStatus.FAILED: frozenset(),
    BookingStatus.CANCELLED: frozenset(),
}

PAYMENT_TRANSITIONS: dict[PaymentStatus, frozenset[PaymentStatus]] = {
    PaymentStatus.PROCESSING: frozenset({PaymentStatus.SUCCESS, PaymentStatus.FAILED}),
    PaymentStatus.SUCCESS: frozenset(),
    PaymentStatus.FAILED: frozenset(),
}

# What a final payment outcome means for its booking.
BOOKING_STATUS_FOR_PAYMENT: dict[PaymentStatus, BookingStatus] = {
    PaymentStatus.SUCCESS: BookingStatus.CONFIRMED,
    PaymentStatus.FAILED: BookingStatus.FAILED,
}


def can_transition_booking(current: BookingStatus, target: BookingStatus) -> bool:
    return target in BOOKING_TRANSITIONS[current]


def can_transition_payment(current: PaymentStatus, target: PaymentStatus) -> bool:
    return target in PAYMENT_TRANSITIONS[current]


def ensure_booking_transition(current: BookingStatus, target: BookingStatus) -> None:
    if not can_transition_booking(current, target):
        raise InvalidStateTransition(
            f"Booking cannot move from {current} to {target}",
            details={"current": current, "target": target},
        )


def ensure_payment_transition(current: PaymentStatus, target: PaymentStatus) -> None:
    if not can_transition_payment(current, target):
        raise InvalidStateTransition(
            f"Payment cannot move from {current} to {target}",
            details={"current": current, "target": target},
        )


def is_booking_final(status: BookingStatus) -> bool:
    return not BOOKING_TRANSITIONS[status]


def is_payment_final(status: PaymentStatus) -> bool:
    return not PAYMENT_TRANSITIONS[status]
