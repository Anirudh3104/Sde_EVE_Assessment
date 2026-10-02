import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.core.deps import AdminUser, DbSession
from app.schemas.catalog import DiagnosticTestCreate, DiagnosticTestOut, DiagnosticTestUpdate
from app.schemas.common import Page, Pagination
from app.services import catalog as catalog_service

router = APIRouter(prefix="/tests", tags=["tests"])

_NOT_FOUND = {404: {"description": "Test not found"}}


@router.get("/", response_model=Page[DiagnosticTestOut], summary="List diagnostic tests")
async def list_tests(
    db: DbSession,
    page: Pagination,
    search: Annotated[
        str | None,
        Query(min_length=1, max_length=100, description="Case-insensitive match on name or code"),
    ] = None,
) -> Page[DiagnosticTestOut]:
    """Active tests in the catalogue, ordered by code. Public."""
    return await catalog_service.list_tests(db, search=search, page=page)


@router.get(
    "/{test_id}",
    response_model=DiagnosticTestOut,
    summary="Get a diagnostic test",
    responses=_NOT_FOUND,
)
async def get_test(test_id: uuid.UUID, db: DbSession) -> DiagnosticTestOut:
    return await catalog_service.get_test(db, test_id)


@router.post(
    "/",
    response_model=DiagnosticTestOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a diagnostic test (admin)",
    responses={409: {"description": "Test code already exists"}},
)
async def create_test(
    payload: DiagnosticTestCreate, db: DbSession, _: AdminUser
) -> DiagnosticTestOut:
    """`code` is stored upper-cased and must be unique."""
    return await catalog_service.create_test(db, payload)


@router.patch(
    "/{test_id}",
    response_model=DiagnosticTestOut,
    summary="Update a diagnostic test (admin)",
    responses={**_NOT_FOUND, 409: {"description": "Test code already exists"}},
)
async def update_test(
    test_id: uuid.UUID, payload: DiagnosticTestUpdate, db: DbSession, _: AdminUser
) -> DiagnosticTestOut:
    """Partial update. Setting `is_active: true` restores a deleted test."""
    return await catalog_service.update_test(db, test_id, payload)


@router.delete(
    "/{test_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    response_model=None,
    summary="Deactivate a diagnostic test (admin)",
    responses=_NOT_FOUND,
)
async def delete_test(test_id: uuid.UUID, db: DbSession, _: AdminUser) -> None:
    """Soft delete: the test is hidden from the catalogue but kept for existing bookings."""
    await catalog_service.deactivate_test(db, test_id)
