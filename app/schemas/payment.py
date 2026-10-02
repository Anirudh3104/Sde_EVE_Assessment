import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.domain.enums import PaymentStatus, WebhookEventResult
from app.schemas.common import ORMModel


class PaymentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    booking_id: uuid.UUID
    simulate_outcome: Literal["success", "failure"] | None = Field(
        default=None,
        description="Force the simulated provider's outcome. Omit for a random outcome.",
    )


class PaymentOut(ORMModel):
    id: uuid.UUID
    booking_id: uuid.UUID
    amount: Decimal
    currency: str
    status: PaymentStatus
    provider_ref: str
    failure_reason: str | None
    created_at: datetime
    updated_at: datetime


class WebhookEventIn(BaseModel):
    """Event the payment provider POSTs to /payments/webhook/. Unknown fields are ignored so the
    provider can add fields without breaking us."""

    event_id: str = Field(min_length=1, max_length=64)
    payment_id: uuid.UUID
    status: Literal["SUCCESS", "FAILED"]
    failure_reason: str | None = Field(default=None, max_length=255)
    occurred_at: AwareDatetime


class WebhookAck(BaseModel):
    event_id: str
    payment_id: uuid.UUID
    result: WebhookEventResult
