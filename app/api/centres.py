import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.core.deps import AdminUser, DbSession
from app.schemas.catalog import (
    CentreCreate,
    CentreDetail,
    CentreOfferingOut,
    CentreOut,
    CentreUpdate,
    OfferingUpsert,
)
from app.schemas.common import Page, Pagination
from app.services import catalog as catalog_service

router = APIRouter(prefix="/centres", tags=["centres"])

_NOT_FOUND = {404: {"description": "Centre not found"}}


@router.get("/", response_model=Page[CentreOut], summary="List diagnostic centres")
async def list_centres(
    db: DbSession,
    page: Pagination,
    city: Annotated[
        str | None, Query(min_length=1, max_length=100, description="Case-insensitive exact match")
    ] = None,
    test_id: Annotated[
        uuid.UUID | None, Query(description="Only centres currently offering this test")
    ] = None,
) -> Page[CentreOut]:
    """Active centres, ordered by name. Public."""
    return await catalog_service.list_centres(db, city=city, test_id=test_id, page=page)


@router.get(
    "/{centre_id}",
    response_model=CentreDetail,
    summary="Get a centre and its tests",
    responses=_NOT_FOUND,
)
async def get_centre(centre_id: uuid.UUID, db: DbSession) -> CentreDetail:
    """Centre details with its price list. Prices are decimal strings, e.g. `"499.00"`."""
    return await catalog_service.get_centre(db, centre_id)


@router.post(
    "/",
    response_model=CentreOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a centre (admin)",
    responses={409: {"description": "A centre with this name already exists in this city"}},
)
async def create_centre(payload: CentreCreate, db: DbSession, _: AdminUser) -> CentreOut:
    return await catalog_service.create_centre(db, payload)


@router.patch(
    "/{centre_id}",
    response_model=CentreOut,
    summary="Update a centre (admin)",
    responses={
        **_NOT_FOUND,
        409: {"description": "A centre with this name already exists in this city"},
    },
)
async def update_centre(
    centre_id: uuid.UUID, payload: CentreUpdate, db: DbSession, _: AdminUser
) -> CentreOut:
    """Partial update. Setting `is_active: true` restores a deleted centre."""
    return await catalog_service.update_centre(db, centre_id, payload)


@router.delete(
    "/{centre_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    response_model=None,
    summary="Deactivate a centre (admin)",
    responses=_NOT_FOUND,
)
async def delete_centre(centre_id: uuid.UUID, db: DbSession, _: AdminUser) -> None:
    """Soft delete: the centre is hidden from the catalogue but kept for existing bookings."""
    await catalog_service.deactivate_centre(db, centre_id)


@router.put(
    "/{centre_id}/tests/{test_id}",
    response_model=CentreOfferingOut,
    summary="Set a test's price at a centre (admin)",
    responses={404: {"description": "Centre or test not found"}},
)
async def upsert_offering(
    centre_id: uuid.UUID,
    test_id: uuid.UUID,
    payload: OfferingUpsert,
    db: DbSession,
    _: AdminUser,
) -> CentreOfferingOut:
    """Create or replace the offering. Idempotent: repeating the request has the same result."""
    return await catalog_service.upsert_offering(db, centre_id, test_id, payload)


@router.delete(
    "/{centre_id}/tests/{test_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    response_model=None,
    summary="Stop offering a test at a centre (admin)",
    responses={404: {"description": "This centre does not offer this test"}},
)
async def remove_offering(
    centre_id: uuid.UUID, test_id: uuid.UUID, db: DbSession, _: AdminUser
) -> None:
    """Marks the offering unavailable (kept for existing bookings and price history)."""
    await catalog_service.remove_offering(db, centre_id, test_id)
