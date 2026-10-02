"""Catalogue business logic: diagnostic tests, centres and per-centre offerings (price list).

Public reads only see active rows and are served cache-aside (see `app.core.cache`). Every
write commits first and then invalidates the catalogue cache. Deletes are soft
(`is_active=False`) because bookings reference centres and tests.
"""

import uuid
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import cached, invalidate_catalog
from app.core.errors import ConflictError, NotFoundError
from app.models.catalog import Centre, CentreTest, DiagnosticTest
from app.schemas.catalog import (
    CentreCreate,
    CentreDetail,
    CentreOfferingOut,
    CentreOut,
    CentreUpdate,
    DiagnosticTestCreate,
    DiagnosticTestOut,
    DiagnosticTestUpdate,
    OfferingUpsert,
)
from app.schemas.common import Page, PaginationParams

# --- helpers ----------------------------------------------------------------------------------


async def _paginate(
    db: AsyncSession, stmt: Select[Any], page: PaginationParams
) -> tuple[list, int]:
    total = await db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    rows = await db.scalars(stmt.limit(page.limit).offset(page.offset))
    return list(rows), total or 0


def _contains(term: str) -> str:
    """ILIKE pattern matching `term` literally (its % and _ are not wildcards)."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def _commit_or_conflict(db: AsyncSession, message: str) -> None:
    """Commit; a unique-constraint violation becomes a 409 (race-safe, the DB is the judge)."""
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError(message) from exc


async def _get_test(db: AsyncSession, test_id: uuid.UUID, *, active_only: bool) -> DiagnosticTest:
    test = await db.get(DiagnosticTest, test_id)
    if test is None or (active_only and not test.is_active):
        raise NotFoundError("Test not found")
    return test


async def _get_centre(db: AsyncSession, centre_id: uuid.UUID, *, active_only: bool) -> Centre:
    centre = await db.get(Centre, centre_id)
    if centre is None or (active_only and not centre.is_active):
        raise NotFoundError("Centre not found")
    return centre


# --- diagnostic tests -------------------------------------------------------------------------

_TEST_CODE_TAKEN = "A test with this code already exists"


async def list_tests(
    db: AsyncSession, *, search: str | None, page: PaginationParams
) -> Page[DiagnosticTestOut]:
    async def load() -> dict[str, Any]:
        stmt = (
            select(DiagnosticTest)
            .where(DiagnosticTest.is_active)
            .order_by(DiagnosticTest.code, DiagnosticTest.id)
        )
        if search:
            pattern = _contains(search)
            stmt = stmt.where(
                or_(DiagnosticTest.name.ilike(pattern), DiagnosticTest.code.ilike(pattern))
            )
        rows, total = await _paginate(db, stmt, page)
        return Page[DiagnosticTestOut](
            items=[DiagnosticTestOut.model_validate(t) for t in rows],
            total=total,
            limit=page.limit,
            offset=page.offset,
        ).model_dump(mode="json")

    key = f"tests:list:search={search or ''}:limit={page.limit}:offset={page.offset}"
    return Page[DiagnosticTestOut].model_validate(await cached(key, load))


async def get_test(db: AsyncSession, test_id: uuid.UUID) -> DiagnosticTestOut:
    return DiagnosticTestOut.model_validate(await _get_test(db, test_id, active_only=True))


async def create_test(db: AsyncSession, data: DiagnosticTestCreate) -> DiagnosticTestOut:
    test = DiagnosticTest(**data.model_dump())
    db.add(test)
    await _commit_or_conflict(db, _TEST_CODE_TAKEN)
    await db.refresh(test)
    await invalidate_catalog()
    return DiagnosticTestOut.model_validate(test)


async def update_test(
    db: AsyncSession, test_id: uuid.UUID, data: DiagnosticTestUpdate
) -> DiagnosticTestOut:
    # Inactive tests can be updated too, so an admin can re-activate one with is_active=true.
    test = await _get_test(db, test_id, active_only=False)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(test, field, value)
    await _commit_or_conflict(db, _TEST_CODE_TAKEN)
    await db.refresh(test)
    await invalidate_catalog()
    return DiagnosticTestOut.model_validate(test)


async def deactivate_test(db: AsyncSession, test_id: uuid.UUID) -> None:
    test = await _get_test(db, test_id, active_only=False)
    test.is_active = False
    await db.commit()
    await invalidate_catalog()


# --- centres ----------------------------------------------------------------------------------

_CENTRE_TAKEN = "A centre with this name already exists in this city"


async def list_centres(
    db: AsyncSession, *, city: str | None, test_id: uuid.UUID | None, page: PaginationParams
) -> Page[CentreOut]:
    city = city.strip().lower() if city else None

    async def load() -> dict[str, Any]:
        stmt = select(Centre).where(Centre.is_active).order_by(Centre.name, Centre.city, Centre.id)
        if city:
            stmt = stmt.where(func.lower(Centre.city) == city)
        if test_id:
            offers_test = (
                select(CentreTest.id)
                .join(DiagnosticTest, DiagnosticTest.id == CentreTest.test_id)
                .where(
                    CentreTest.centre_id == Centre.id,
                    CentreTest.test_id == test_id,
                    CentreTest.is_available,
                    DiagnosticTest.is_active,
                )
            )
            stmt = stmt.where(offers_test.exists())
        rows, total = await _paginate(db, stmt, page)
        return Page[CentreOut](
            items=[CentreOut.model_validate(c) for c in rows],
            total=total,
            limit=page.limit,
            offset=page.offset,
        ).model_dump(mode="json")

    key = (
        f"centres:list:city={city or ''}:test_id={test_id or ''}"
        f":limit={page.limit}:offset={page.offset}"
    )
    return Page[CentreOut].model_validate(await cached(key, load))


async def get_centre(db: AsyncSession, centre_id: uuid.UUID) -> CentreDetail:
    """Centre with its price list (offerings of active tests, available or not)."""

    async def load() -> dict[str, Any]:
        centre = await _get_centre(db, centre_id, active_only=True)
        rows = await db.execute(
            select(
                CentreTest.test_id,
                DiagnosticTest.code,
                DiagnosticTest.name,
                CentreTest.price,
                CentreTest.is_available,
            )
            .join(DiagnosticTest, DiagnosticTest.id == CentreTest.test_id)
            .where(CentreTest.centre_id == centre.id, DiagnosticTest.is_active)
            .order_by(DiagnosticTest.name)
        )
        tests = [CentreOfferingOut.model_validate(row, from_attributes=True) for row in rows]
        return CentreDetail(
            **CentreOut.model_validate(centre).model_dump(), tests=tests
        ).model_dump(mode="json")

    return CentreDetail.model_validate(await cached(f"centres:detail:{centre_id}", load))


async def create_centre(db: AsyncSession, data: CentreCreate) -> CentreOut:
    centre = Centre(**data.model_dump())
    db.add(centre)
    await _commit_or_conflict(db, _CENTRE_TAKEN)
    await db.refresh(centre)
    await invalidate_catalog()
    return CentreOut.model_validate(centre)


async def update_centre(db: AsyncSession, centre_id: uuid.UUID, data: CentreUpdate) -> CentreOut:
    # Inactive centres can be updated too, so an admin can re-activate one with is_active=true.
    centre = await _get_centre(db, centre_id, active_only=False)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(centre, field, value)
    await _commit_or_conflict(db, _CENTRE_TAKEN)
    await db.refresh(centre)
    await invalidate_catalog()
    return CentreOut.model_validate(centre)


async def deactivate_centre(db: AsyncSession, centre_id: uuid.UUID) -> None:
    centre = await _get_centre(db, centre_id, active_only=False)
    centre.is_active = False
    await db.commit()
    await invalidate_catalog()


# --- offerings (per-centre price list) --------------------------------------------------------


async def upsert_offering(
    db: AsyncSession, centre_id: uuid.UUID, test_id: uuid.UUID, data: OfferingUpsert
) -> CentreOfferingOut:
    """Create or replace the price/availability of a test at a centre (atomic ON CONFLICT)."""
    centre = await _get_centre(db, centre_id, active_only=True)
    test = await _get_test(db, test_id, active_only=True)
    stmt = (
        insert(CentreTest)
        .values(
            id=uuid.uuid4(),
            centre_id=centre.id,
            test_id=test.id,
            price=data.price,
            is_available=data.is_available,
        )
        .on_conflict_do_update(
            index_elements=[CentreTest.centre_id, CentreTest.test_id],
            set_={"price": data.price, "is_available": data.is_available, "updated_at": func.now()},
        )
        .returning(CentreTest.price, CentreTest.is_available)
    )
    offering = (await db.execute(stmt)).one()
    await db.commit()
    await invalidate_catalog()
    return CentreOfferingOut(
        test_id=test.id,
        code=test.code,
        name=test.name,
        price=offering.price,
        is_available=offering.is_available,
    )


async def remove_offering(db: AsyncSession, centre_id: uuid.UUID, test_id: uuid.UUID) -> None:
    """Stop offering a test at a centre. Soft (is_available=False): bookings may reference it."""
    offering = await db.scalar(
        select(CentreTest).where(CentreTest.centre_id == centre_id, CentreTest.test_id == test_id)
    )
    if offering is None:
        raise NotFoundError("This centre does not offer this test")
    offering.is_available = False
    await db.commit()
    await invalidate_catalog()
