import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import AliasPath, AwareDatetime, BaseModel, ConfigDict, Field

from app.domain.enums import BookingStatus
from app.schemas.common import ORMModel


class BookingCreate(BaseModel):
    """The client chooses *what* and *when*; the price is always taken from the catalogue."""

    model_config = ConfigDict(extra="forbid")

    centre_id: uuid.UUID
    test_id: uuid.UUID
    appointment_at: AwareDatetime = Field(
        description="Appointment time with an explicit UTC offset, e.g. 2026-10-05T09:00:00+05:30"
    )


class BookingOut(ORMModel):
    id: uuid.UUID
    centre_id: uuid.UUID
    test_id: uuid.UUID
    # Read from the eagerly loaded `centre` / `test` relationships.
    centre_name: str = Field(validation_alias=AliasPath("centre", "name"))
    test_name: str = Field(validation_alias=AliasPath("test", "name"))
    appointment_at: datetime
    amount: Decimal
    currency: str
    status: BookingStatus
    cancelled_at: datetime | None
    created_at: datetime
    updated_at: datetime
