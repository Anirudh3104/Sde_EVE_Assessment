import itertools

import pytest

from app.core.errors import InvalidStateTransition
from app.domain.enums import BookingStatus, PaymentStatus
from app.domain.state_machine import (
    BOOKING_STATUS_FOR_PAYMENT,
    can_transition_booking,
    can_transition_payment,
    ensure_booking_transition,
    ensure_payment_transition,
    is_booking_final,
    is_payment_final,
)

B = BookingStatus
P = PaymentStatus

ALLOWED_BOOKING = {
    (B.PENDING, B.CONFIRMED),
    (B.PENDING, B.FAILED),
    (B.PENDING, B.CANCELLED),
    (B.CONFIRMED, B.CANCELLED),
}
ALLOWED_PAYMENT = {(P.PROCESSING, P.SUCCESS), (P.PROCESSING, P.FAILED)}

ALL_BOOKING_PAIRS = list(itertools.product(B, repeat=2))
ALL_PAYMENT_PAIRS = list(itertools.product(P, repeat=2))


@pytest.mark.parametrize(("current", "target"), ALL_BOOKING_PAIRS)
def test_booking_transitions(current: BookingStatus, target: BookingStatus) -> None:
    allowed = (current, target) in ALLOWED_BOOKING
    assert can_transition_booking(current, target) is allowed
    if allowed:
        ensure_booking_transition(current, target)
    else:
        with pytest.raises(InvalidStateTransition) as exc_info:
            ensure_booking_transition(current, target)
        assert exc_info.value.status_code == 409
        assert exc_info.value.code == "invalid_state_transition"
        assert exc_info.value.details == {"current": current, "target": target}


@pytest.mark.parametrize(("current", "target"), ALL_PAYMENT_PAIRS)
def test_payment_transitions(current: PaymentStatus, target: PaymentStatus) -> None:
    allowed = (current, target) in ALLOWED_PAYMENT
    assert can_transition_payment(current, target) is allowed
    if allowed:
        ensure_payment_transition(current, target)
    else:
        with pytest.raises(InvalidStateTransition):
            ensure_payment_transition(current, target)


@pytest.mark.parametrize(
    ("status", "final"),
    [(B.PENDING, False), (B.CONFIRMED, False), (B.FAILED, True), (B.CANCELLED, True)],
)
def test_is_booking_final(status: BookingStatus, final: bool) -> None:
    assert is_booking_final(status) is final


@pytest.mark.parametrize(
    ("status", "final"), [(P.PROCESSING, False), (P.SUCCESS, True), (P.FAILED, True)]
)
def test_is_payment_final(status: PaymentStatus, final: bool) -> None:
    assert is_payment_final(status) is final


def test_booking_status_for_payment_outcome() -> None:
    assert BOOKING_STATUS_FOR_PAYMENT == {P.SUCCESS: B.CONFIRMED, P.FAILED: B.FAILED}
    # PROCESSING is not an outcome, so it must not drive a booking change.
    assert P.PROCESSING not in BOOKING_STATUS_FOR_PAYMENT


@pytest.mark.parametrize("outcome", list(BOOKING_STATUS_FOR_PAYMENT))
def test_payment_outcome_is_a_legal_move_from_pending(outcome: PaymentStatus) -> None:
    """A payment only ever resolves a PENDING booking, so each mapped target must be reachable."""
    ensure_payment_transition(P.PROCESSING, outcome)
    ensure_booking_transition(B.PENDING, BOOKING_STATUS_FOR_PAYMENT[outcome])
