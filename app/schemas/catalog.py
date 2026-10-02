"""Catalogue schemas: diagnostic tests, centres and the per-centre price list.

Money is `Decimal` end to end (never float). Pydantic serialises Decimal to a JSON *string*
(e.g. `"499.00"`), which keeps the exact value for clients; inputs accept either a number or a
string, must be > 0 and have at most 2 decimal places.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import ORMModel

Price = Annotated[Decimal, Field(gt=0, max_digits=10, decimal_places=2, examples=["499.00"])]


def _not_null(value: object) -> object:
    """PATCH bodies may omit a field, but may not null out a required column."""
    if value is None:
        raise ValueError("must not be null")
    return value


def _strip(value: object) -> object:
    return value.strip() if isinstance(value, str) else value


# --- diagnostic tests -------------------------------------------------------------------------


class DiagnosticTestCreate(BaseModel):
    code: str = Field(min_length=1, max_length=32, examples=["CBC"])
    name: str = Field(min_length=1, max_length=200, examples=["Complete Blood Count"])
    description: str | None = None

    _strip_fields = field_validator("code", "name", mode="before")(_strip)

    @field_validator("code")
    @classmethod
    def _upper_code(cls, value: str | None) -> str | None:
        return value.upper() if value is not None else value


class DiagnosticTestUpdate(BaseModel):
    """Partial update; only fields present in the body are changed."""

    code: str | None = Field(default=None, min_length=1, max_length=32)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    is_active: bool | None = None

    _strip_fields = field_validator("code", "name", mode="before")(_strip)
    _reject_null = field_validator("code", "name", "is_active")(_not_null)

    @field_validator("code")
    @classmethod
    def _upper_code(cls, value: str | None) -> str | None:
        return value.upper() if value is not None else value


class DiagnosticTestOut(ORMModel):
    id: uuid.UUID
    code: str
    name: str
    description: str | None
    is_active: bool
    created_at: datetime


# --- centres ----------------------------------------------------------------------------------


class CentreCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200, examples=["Apollo Diagnostics"])
    address: str = Field(min_length=1, max_length=500)
    city: str = Field(min_length=1, max_length=100, examples=["Bengaluru"])
    pincode: str | None = Field(default=None, max_length=10)

    _strip_fields = field_validator("name", "address", "city", "pincode", mode="before")(_strip)


class CentreUpdate(BaseModel):
    """Partial update; only fields present in the body are changed."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    address: str | None = Field(default=None, min_length=1, max_length=500)
    city: str | None = Field(default=None, min_length=1, max_length=100)
    pincode: str | None = Field(default=None, max_length=10)
    is_active: bool | None = None

    _strip_fields = field_validator("name", "address", "city", "pincode", mode="before")(_strip)
    _reject_null = field_validator("name", "address", "city", "is_active")(_not_null)


class CentreOut(ORMModel):
    id: uuid.UUID
    name: str
    address: str
    city: str
    pincode: str | None
    is_active: bool
    created_at: datetime


class CentreOfferingOut(BaseModel):
    """A test offered at a centre, with that centre's price."""

    test_id: uuid.UUID
    code: str
    name: str
    price: Decimal
    is_available: bool


class CentreDetail(CentreOut):
    tests: list[CentreOfferingOut]


class OfferingUpsert(BaseModel):
    price: Price
    is_available: bool = True
