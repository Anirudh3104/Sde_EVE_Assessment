import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from httpx import AsyncClient, Response
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import BookingStatus, PaymentStatus, RefundStatus
from app.models import Booking, Payment
from tests.conftest import AuthedUser, Catalog, future_slot


def booking_body(catalog: Catalog, **overrides: Any) -> dict[str, Any]:
    body = {
        "centre_id": str(catalog.centre.id),
        "test_id": str(catalog.test.id),
        "appointment_at": future_slot(),
    }
    body.update(overrides)
    return body


async def book(client: AsyncClient, who: AuthedUser, catalog: Catalog, **overrides: Any) -> dict:
    resp = await client.post(
        "/bookings/", json=booking_body(catalog, **overrides), headers=who.headers
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def cancel(client: AsyncClient, who: AuthedUser, booking_id: str) -> Response:
    return await client.post(f"/bookings/{booking_id}/cancel", headers=who.headers)


async def set_booking(db: AsyncSession, booking_id: str, **values: Any) -> None:
    """Move a booking into a state the API can't reach on its own (e.g. paid or failed)."""
    await db.execute(update(Booking).where(Booking.id == uuid.UUID(booking_id)).values(**values))
    await db.commit()


async def add_payment(
    db: AsyncSession, booking: dict, who: AuthedUser, status: PaymentStatus
) -> Payment:
    payment = Payment(
        booking_id=uuid.UUID(booking["id"]),
        user_id=who.user.id,
        amount=Decimal(booking["amount"]),
        status=status,
        idempotency_key=f"key-{uuid.uuid4().hex}",
        request_hash="0" * 64,
        provider_ref=f"prov-{uuid.uuid4().hex[:16]}",
    )
    db.add(payment)
    await db.commit()
    return payment


def error_code(resp: Response) -> str:
    return resp.json()["error"]["code"]


# --- create -----------------------------------------------------------------------------------


async def test_create_booking_uses_server_price(
    client: AsyncClient, patient: AuthedUser, catalog: Catalog
) -> None:
    data = await book(client, patient, catalog)

    assert data["status"] == "PENDING"
    assert Decimal(data["amount"]) == Decimal("499.00")
    assert data["currency"] == "INR"
    assert data["centre_id"] == str(catalog.centre.id)
    assert data["test_id"] == str(catalog.test.id)
    assert data["centre_name"] == "Apollo Diagnostics"
    assert data["test_name"] == "Complete Blood Count"
    assert data["cancelled_at"] is None


async def test_create_rejects_client_supplied_amount(
    client: AsyncClient, patient: AuthedUser, catalog: Catalog
) -> None:
    resp = await client.post(
        "/bookings/", json=booking_body(catalog, amount="1.00"), headers=patient.headers
    )
    assert resp.status_code == 422
    assert error_code(resp) == "validation_error"


async def test_create_rejects_past_time(
    client: AsyncClient, patient: AuthedUser, catalog: Catalog
) -> None:
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    resp = await client.post(
        "/bookings/", json=booking_body(catalog, appointment_at=past), headers=patient.headers
    )
    assert resp.status_code == 422
    assert error_code(resp) == "appointment_in_past"


async def test_create_rejects_time_beyond_horizon(
    client: AsyncClient, patient: AuthedUser, catalog: Catalog
) -> None:
    resp = await client.post(
        "/bookings/",
        json=booking_body(catalog, appointment_at=future_slot(days=120)),
        headers=patient.headers,
    )
    assert resp.status_code == 422
    assert error_code(resp) == "appointment_too_far"


async def test_create_rejects_naive_datetime(
    client: AsyncClient, patient: AuthedUser, catalog: Catalog
) -> None:
    naive = (datetime.now() + timedelta(days=2)).replace(microsecond=0).isoformat()
    resp = await client.post(
        "/bookings/", json=booking_body(catalog, appointment_at=naive), headers=patient.headers
    )
    assert resp.status_code == 422


async def test_create_unknown_centre_404(
    client: AsyncClient, patient: AuthedUser, catalog: Catalog
) -> None:
    resp = await client.post(
        "/bookings/",
        json=booking_body(catalog, centre_id=str(uuid.uuid4())),
        headers=patient.headers,
    )
    assert resp.status_code == 404


async def test_create_inactive_centre_404(
    client: AsyncClient, db: AsyncSession, patient: AuthedUser, catalog: Catalog
) -> None:
    catalog.centre.is_active = False
    await db.commit()

    resp = await client.post("/bookings/", json=booking_body(catalog), headers=patient.headers)
    assert resp.status_code == 404


async def test_create_unavailable_test_422(
    client: AsyncClient, patient: AuthedUser, catalog: Catalog
) -> None:
    resp = await client.post(
        "/bookings/",
        json=booking_body(catalog, test_id=str(catalog.unavailable_test.id)),
        headers=patient.headers,
    )
    assert resp.status_code == 422
    assert error_code(resp) == "test_not_offered"


async def test_create_unoffered_test_422(
    client: AsyncClient, patient: AuthedUser, catalog: Catalog
) -> None:
    for test_id in (catalog.unoffered_test.id, uuid.uuid4()):
        resp = await client.post(
            "/bookings/", json=booking_body(catalog, test_id=str(test_id)), headers=patient.headers
        )
        assert resp.status_code == 422
        assert error_code(resp) == "test_not_offered"


async def test_create_inactive_test_422(
    client: AsyncClient, db: AsyncSession, patient: AuthedUser, catalog: Catalog
) -> None:
    catalog.test.is_active = False
    await db.commit()

    resp = await client.post("/bookings/", json=booking_body(catalog), headers=patient.headers)
    assert resp.status_code == 422
    assert error_code(resp) == "test_not_offered"


async def test_create_requires_auth(client: AsyncClient, catalog: Catalog) -> None:
    resp = await client.post("/bookings/", json=booking_body(catalog))
    assert resp.status_code == 401


async def test_duplicate_active_booking_409(
    client: AsyncClient, patient: AuthedUser, catalog: Catalog
) -> None:
    slot = future_slot()
    await book(client, patient, catalog, appointment_at=slot)

    resp = await client.post(
        "/bookings/", json=booking_body(catalog, appointment_at=slot), headers=patient.headers
    )
    assert resp.status_code == 409
    assert error_code(resp) == "duplicate_booking"


async def test_same_slot_for_different_users_is_allowed(
    client: AsyncClient, patient: AuthedUser, other_patient: AuthedUser, catalog: Catalog
) -> None:
    slot = future_slot()
    await book(client, patient, catalog, appointment_at=slot)
    await book(client, other_patient, catalog, appointment_at=slot)


async def test_rebook_allowed_after_cancel(
    client: AsyncClient, patient: AuthedUser, catalog: Catalog
) -> None:
    slot = future_slot()
    first = await book(client, patient, catalog, appointment_at=slot)
    assert (await cancel(client, patient, first["id"])).status_code == 200

    second = await book(client, patient, catalog, appointment_at=slot)
    assert second["id"] != first["id"]


# --- list & get -------------------------------------------------------------------------------


async def test_list_shows_only_own_bookings(
    client: AsyncClient, patient: AuthedUser, other_patient: AuthedUser, catalog: Catalog
) -> None:
    mine = await book(client, patient, catalog)
    await book(client, other_patient, catalog)

    resp = await client.get("/bookings/", headers=patient.headers)
    assert resp.status_code == 200
    page = resp.json()
    assert page["total"] == 1
    assert [b["id"] for b in page["items"]] == [mine["id"]]


async def test_admin_lists_all_bookings(
    client: AsyncClient,
    patient: AuthedUser,
    other_patient: AuthedUser,
    admin: AuthedUser,
    catalog: Catalog,
) -> None:
    await book(client, patient, catalog)
    await book(client, other_patient, catalog)

    resp = await client.get("/bookings/", headers=admin.headers)
    assert resp.json()["total"] == 2


async def test_list_filters_by_status(
    client: AsyncClient, patient: AuthedUser, catalog: Catalog
) -> None:
    kept = await book(client, patient, catalog, appointment_at=future_slot(days=1))
    dropped = await book(client, patient, catalog, appointment_at=future_slot(days=2))
    await cancel(client, patient, dropped["id"])

    pending = (await client.get("/bookings/?status=PENDING", headers=patient.headers)).json()
    cancelled = (await client.get("/bookings/?status=CANCELLED", headers=patient.headers)).json()
    assert [b["id"] for b in pending["items"]] == [kept["id"]]
    assert [b["id"] for b in cancelled["items"]] == [dropped["id"]]

    resp = await client.get("/bookings/?status=BOGUS", headers=patient.headers)
    assert resp.status_code == 422


async def test_list_paginates_newest_first(
    client: AsyncClient, patient: AuthedUser, catalog: Catalog
) -> None:
    ids = [
        (await book(client, patient, catalog, appointment_at=future_slot(days=d)))["id"]
        for d in (1, 2, 3)
    ]

    first = (await client.get("/bookings/?limit=2&offset=0", headers=patient.headers)).json()
    second = (await client.get("/bookings/?limit=2&offset=2", headers=patient.headers)).json()
    assert first["total"] == second["total"] == 3
    assert (first["limit"], first["offset"]) == (2, 0)
    assert [b["id"] for b in first["items"] + second["items"]] == ids[::-1]

    resp = await client.get("/bookings/?limit=0", headers=patient.headers)
    assert resp.status_code == 422


async def test_get_own_booking(client: AsyncClient, patient: AuthedUser, catalog: Catalog) -> None:
    created = await book(client, patient, catalog)

    resp = await client.get(f"/bookings/{created['id']}", headers=patient.headers)
    assert resp.status_code == 200
    assert resp.json() == created


async def test_get_other_users_booking_404(
    client: AsyncClient, patient: AuthedUser, other_patient: AuthedUser, catalog: Catalog
) -> None:
    created = await book(client, patient, catalog)

    resp = await client.get(f"/bookings/{created['id']}", headers=other_patient.headers)
    assert resp.status_code == 404


async def test_admin_can_get_any_booking(
    client: AsyncClient, patient: AuthedUser, admin: AuthedUser, catalog: Catalog
) -> None:
    created = await book(client, patient, catalog)

    resp = await client.get(f"/bookings/{created['id']}", headers=admin.headers)
    assert resp.status_code == 200


async def test_malformed_booking_id_422(client: AsyncClient, patient: AuthedUser) -> None:
    assert (await client.get("/bookings/not-a-uuid", headers=patient.headers)).status_code == 422
    assert (await cancel(client, patient, "not-a-uuid")).status_code == 422


# --- cancel -----------------------------------------------------------------------------------


async def test_cancel_pending_booking(
    client: AsyncClient, patient: AuthedUser, catalog: Catalog
) -> None:
    created = await book(client, patient, catalog)

    resp = await cancel(client, patient, created["id"])
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "CANCELLED"
    assert data["cancelled_at"] is not None


async def test_cancel_twice_409(client: AsyncClient, patient: AuthedUser, catalog: Catalog) -> None:
    created = await book(client, patient, catalog)
    await cancel(client, patient, created["id"])

    resp = await cancel(client, patient, created["id"])
    assert resp.status_code == 409
    assert error_code(resp) == "invalid_state_transition"


async def test_cancel_other_users_booking_404(
    client: AsyncClient,
    patient: AuthedUser,
    other_patient: AuthedUser,
    admin: AuthedUser,
    catalog: Catalog,
) -> None:
    created = await book(client, patient, catalog)

    assert (await cancel(client, other_patient, created["id"])).status_code == 404
    assert (await cancel(client, admin, created["id"])).status_code == 404  # owner-only action
    assert (await cancel(client, patient, str(uuid.uuid4()))).status_code == 404


async def test_cancel_confirmed_booking_marks_refund_pending(
    client: AsyncClient, db: AsyncSession, patient: AuthedUser, catalog: Catalog
) -> None:
    created = await book(client, patient, catalog)
    await set_booking(db, created["id"], status=BookingStatus.CONFIRMED)
    payment = await add_payment(db, created, patient, PaymentStatus.SUCCESS)

    resp = await cancel(client, patient, created["id"])
    assert resp.status_code == 200
    assert resp.json()["status"] == "CANCELLED"

    await db.refresh(payment)
    assert payment.refund_status == RefundStatus.REFUND_PENDING
    assert payment.status == PaymentStatus.SUCCESS


async def test_cancel_while_payment_processing_409(
    client: AsyncClient, db: AsyncSession, patient: AuthedUser, catalog: Catalog
) -> None:
    created = await book(client, patient, catalog)
    await add_payment(db, created, patient, PaymentStatus.PROCESSING)

    resp = await cancel(client, patient, created["id"])
    assert resp.status_code == 409
    assert error_code(resp) == "payment_in_progress"


async def test_cancel_failed_booking_409(
    client: AsyncClient, db: AsyncSession, patient: AuthedUser, catalog: Catalog
) -> None:
    created = await book(client, patient, catalog)
    await set_booking(db, created["id"], status=BookingStatus.FAILED)

    resp = await cancel(client, patient, created["id"])
    assert resp.status_code == 409
    assert error_code(resp) == "invalid_state_transition"


async def test_cancel_after_appointment_passed_409(
    client: AsyncClient, db: AsyncSession, patient: AuthedUser, catalog: Catalog
) -> None:
    created = await book(client, patient, catalog)
    await set_booking(db, created["id"], appointment_at=datetime.now(UTC) - timedelta(minutes=5))

    resp = await cancel(client, patient, created["id"])
    assert resp.status_code == 409
    assert error_code(resp) == "appointment_passed"
