import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.domain.enums import PaymentStatus, RefundStatus, WebhookEventResult
from app.models.booking import Booking


class Payment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "payments"
    __table_args__ = (
        # Replaying POST /payments/ with the same key returns the original payment.
        UniqueConstraint("user_id", "idempotency_key", name="uq_payments_user_idempotency_key"),
    )

    # One payment per booking: a FAILED booking is final, so a booking is never charged twice.
    booking_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bookings.id", ondelete="RESTRICT"), unique=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    currency: Mapped[str] = mapped_column(String(3), default="INR", server_default="INR")
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, name="payment_status"),
        default=PaymentStatus.PROCESSING,
        server_default="PROCESSING",
    )
    idempotency_key: Mapped[str] = mapped_column(String(255))
    # SHA-256 of the request body, to reject a reused key carrying a different request.
    request_hash: Mapped[str] = mapped_column(String(64))
    # Reference the (simulated) provider uses for this payment.
    provider_ref: Mapped[str] = mapped_column(String(64), unique=True)
    failure_reason: Mapped[str | None] = mapped_column(String(255))
    refund_status: Mapped[RefundStatus | None] = mapped_column(
        Enum(RefundStatus, name="refund_status")
    )

    booking: Mapped[Booking] = relationship()


class WebhookEvent(UUIDPrimaryKeyMixin, Base):
    """Every distinct provider event we have received. `event_id` uniqueness is the idempotency
    guarantee for POST /payments/webhook/."""

    __tablename__ = "webhook_events"

    event_id: Mapped[str] = mapped_column(String(64), unique=True)
    payment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("payments.id", ondelete="RESTRICT"), index=True
    )
    status: Mapped[PaymentStatus] = mapped_column(Enum(PaymentStatus, name="payment_status"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    signature_valid: Mapped[bool] = mapped_column(Boolean)
    result: Mapped[WebhookEventResult | None] = mapped_column(
        Enum(WebhookEventResult, name="webhook_event_result")
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
