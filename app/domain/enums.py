from enum import StrEnum


class UserRole(StrEnum):
    PATIENT = "PATIENT"
    ADMIN = "ADMIN"


class BookingStatus(StrEnum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class PaymentStatus(StrEnum):
    PROCESSING = "PROCESSING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class RefundStatus(StrEnum):
    REFUND_PENDING = "REFUND_PENDING"


class WebhookEventResult(StrEnum):
    APPLIED = "APPLIED"  # event changed payment/booking state
    DUPLICATE = "DUPLICATE"  # same event_id seen before (not persisted twice; for responses)
    IGNORED = "IGNORED"  # new event, but payment was already final


ACTIVE_BOOKING_STATUSES = (BookingStatus.PENDING, BookingStatus.CONFIRMED)
