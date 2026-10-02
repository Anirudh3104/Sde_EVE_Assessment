import uuid
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Centre(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "centres"
    __table_args__ = (UniqueConstraint("name", "city", name="uq_centres_name_city"),)

    name: Mapped[str] = mapped_column(String(200))
    address: Mapped[str] = mapped_column(String(500))
    city: Mapped[str] = mapped_column(String(100), index=True)
    pincode: Mapped[str | None] = mapped_column(String(10))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    offerings: Mapped[list["CentreTest"]] = relationship(back_populates="centre")


class DiagnosticTest(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A test in the global catalogue (e.g. CBC). Prices live per centre in CentreTest."""

    __tablename__ = "diagnostic_tests"

    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    offerings: Mapped[list["CentreTest"]] = relationship(back_populates="test")


class CentreTest(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A test offered at a specific centre, with that centre's price."""

    __tablename__ = "centre_tests"
    __table_args__ = (
        UniqueConstraint("centre_id", "test_id", name="uq_centre_tests_centre_test"),
        CheckConstraint("price > 0", name="price_positive"),
    )

    centre_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("centres.id", ondelete="RESTRICT"), index=True
    )
    test_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("diagnostic_tests.id", ondelete="RESTRICT"), index=True
    )
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    is_available: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    centre: Mapped[Centre] = relationship(back_populates="offerings")
    test: Mapped[DiagnosticTest] = relationship(back_populates="offerings")
