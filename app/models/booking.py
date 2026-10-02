import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, Index, Numeric, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.domain.enums import BookingStatus
from app.models.catalog import Centre, DiagnosticTest
from app.models.user import User


class Booking(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "bookings"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        # A user cannot hold two *active* bookings for the same test/centre/slot.
        # FAILED and CANCELLED bookings don't block re-booking the same slot.
        Index(
            "uq_bookings_active_slot",
            "user_id",
            "centre_id",
            "test_id",
            "appointment_at",
            unique=True,
            postgresql_where=text("status IN ('PENDING', 'CONFIRMED')"),
        ),
        Index("ix_bookings_user_created", "user_id", "created_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    centre_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("centres.id", ondelete="RESTRICT"), index=True
    )
    test_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("diagnostic_tests.id", ondelete="RESTRICT"), index=True
    )
    appointment_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Snapshot of centre_tests.price at booking time; later price changes don't affect it.
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    currency: Mapped[str] = mapped_column(String(3), default="INR", server_default="INR")
    status: Mapped[BookingStatus] = mapped_column(
        Enum(BookingStatus, name="booking_status"),
        default=BookingStatus.PENDING,
        server_default="PENDING",
        index=True,
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship()
    centre: Mapped[Centre] = relationship()
    test: Mapped[DiagnosticTest] = relationship()
