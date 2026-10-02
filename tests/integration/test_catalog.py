import contextlib
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import get_redis
from app.models import Centre
from tests.conftest import AuthedUser, Catalog

NEW_TEST = {"code": " hba1c ", "name": "Glycated Haemoglobin", "description": "3-month average"}
NEW_CENTRE = {"name": "Metro Labs", "address": "22 Park Street", "city": "Kolkata"}


# --- access control ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/tests/"),
        ("patch", "/tests/{test_id}"),
        ("delete", "/tests/{test_id}"),
        ("post", "/centres/"),
        ("patch", "/centres/{centre_id}"),
        ("delete", "/centres/{centre_id}"),
        ("put", "/centres/{centre_id}/tests/{test_id}"),
        ("delete", "/centres/{centre_id}/tests/{test_id}"),
    ],
)
async def test_catalogue_writes_require_admin(
    client: AsyncClient, patient: AuthedUser, catalog: Catalog, method: str, path: str
) -> None:
    url = path.format(test_id=catalog.test.id, centre_id=catalog.centre.id)
    body = {} if method in ("post", "patch", "put") else None
    kwargs = {"json": body} if body is not None else {}

    anonymous = await client.request(method, url, **kwargs)
    as_patient = await client.request(method, url, headers=patient.headers, **kwargs)

    assert anonymous.status_code == 401
    assert as_patient.status_code == 403
    assert as_patient.json()["error"]["code"] == "forbidden"


async def test_catalogue_reads_are_public(client: AsyncClient, catalog: Catalog) -> None:
    for url in (
        "/tests/",
        f"/tests/{catalog.test.id}",
        "/centres/",
        f"/centres/{catalog.centre.id}",
    ):
        assert (await client.get(url)).status_code == 200, url


# --- diagnostic tests -------------------------------------------------------------------------


async def test_admin_test_crud(client: AsyncClient, admin: AuthedUser) -> None:
    created = await client.post("/tests/", json=NEW_TEST, headers=admin.headers)
    assert created.status_code == 201
    test = created.json()
    assert test["code"] == "HBA1C"
    assert test["is_active"] is True

    fetched = await client.get(f"/tests/{test['id']}")
    assert fetched.json() == test

    patched = await client.patch(
        f"/tests/{test['id']}", json={"name": "HbA1c"}, headers=admin.headers
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "HbA1c"
    assert patched.json()["description"] == "3-month average"  # untouched by partial update

    deleted = await client.delete(f"/tests/{test['id']}", headers=admin.headers)
    assert deleted.status_code == 204
    assert deleted.content == b""
    assert (await client.get(f"/tests/{test['id']}")).status_code == 404


async def test_duplicate_test_code_conflicts(client: AsyncClient, admin: AuthedUser) -> None:
    await client.post("/tests/", json=NEW_TEST, headers=admin.headers)

    response = await client.post(
        "/tests/", json={**NEW_TEST, "code": "HbA1c"}, headers=admin.headers
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


async def test_patch_test_code_to_existing_code_conflicts(
    client: AsyncClient, admin: AuthedUser, catalog: Catalog
) -> None:
    response = await client.patch(
        f"/tests/{catalog.test.id}", json={"code": "lipid"}, headers=admin.headers
    )

    assert response.status_code == 409


async def test_patch_rejects_null_for_required_field(
    client: AsyncClient, admin: AuthedUser, catalog: Catalog
) -> None:
    response = await client.patch(
        f"/tests/{catalog.test.id}", json={"name": None}, headers=admin.headers
    )

    assert response.status_code == 422


async def test_soft_deleted_test_is_hidden_from_list(
    client: AsyncClient, admin: AuthedUser, catalog: Catalog
) -> None:
    await client.delete(f"/tests/{catalog.test.id}", headers=admin.headers)

    codes = [t["code"] for t in (await client.get("/tests/")).json()["items"]]

    assert codes == ["LIPID", "TSH"]


async def test_list_tests_search_matches_name_or_code(
    client: AsyncClient, catalog: Catalog
) -> None:
    by_name = (await client.get("/tests/", params={"search": "blood"})).json()
    by_code = (await client.get("/tests/", params={"search": "tsh"})).json()
    no_wildcards = (await client.get("/tests/", params={"search": "%"})).json()

    assert [t["code"] for t in by_name["items"]] == ["CBC"]
    assert [t["code"] for t in by_code["items"]] == ["TSH"]
    assert no_wildcards["total"] == 0


async def test_list_tests_pagination(client: AsyncClient, catalog: Catalog) -> None:
    response = await client.get("/tests/", params={"limit": 2, "offset": 1})

    body = response.json()
    assert body["total"] == 3
    assert body["limit"] == 2
    assert body["offset"] == 1
    assert [t["code"] for t in body["items"]] == ["LIPID", "TSH"]


async def test_invalid_pagination_is_rejected(client: AsyncClient) -> None:
    assert (await client.get("/tests/", params={"limit": 0})).status_code == 422
    assert (await client.get("/tests/", params={"limit": 101})).status_code == 422
    assert (await client.get("/tests/", params={"offset": -1})).status_code == 422


async def test_unknown_test_is_404(client: AsyncClient, admin: AuthedUser) -> None:
    missing = uuid.uuid4()

    assert (await client.get(f"/tests/{missing}")).status_code == 404
    patch = await client.patch(f"/tests/{missing}", json={"name": "x"}, headers=admin.headers)
    assert patch.status_code == 404
    assert (await client.delete(f"/tests/{missing}", headers=admin.headers)).status_code == 404


async def test_malformed_id_is_422(client: AsyncClient) -> None:
    assert (await client.get("/tests/not-a-uuid")).status_code == 422


# --- centres ----------------------------------------------------------------------------------


async def test_admin_centre_crud(client: AsyncClient, admin: AuthedUser) -> None:
    created = await client.post("/centres/", json=NEW_CENTRE, headers=admin.headers)
    assert created.status_code == 201
    centre = created.json()
    assert centre["city"] == "Kolkata"
    assert centre["pincode"] is None

    patched = await client.patch(
        f"/centres/{centre['id']}", json={"pincode": "700016"}, headers=admin.headers
    )
    assert patched.status_code == 200
    assert patched.json()["pincode"] == "700016"
    assert patched.json()["name"] == "Metro Labs"

    detail = await client.get(f"/centres/{centre['id']}")
    assert detail.status_code == 200
    assert detail.json()["tests"] == []

    assert (
        await client.delete(f"/centres/{centre['id']}", headers=admin.headers)
    ).status_code == 204
    assert (await client.get(f"/centres/{centre['id']}")).status_code == 404


async def test_duplicate_centre_in_same_city_conflicts(
    client: AsyncClient, admin: AuthedUser
) -> None:
    await client.post("/centres/", json=NEW_CENTRE, headers=admin.headers)

    same_city = await client.post("/centres/", json=NEW_CENTRE, headers=admin.headers)
    other_city = await client.post(
        "/centres/", json={**NEW_CENTRE, "city": "Pune"}, headers=admin.headers
    )

    assert same_city.status_code == 409
    assert other_city.status_code == 201


async def test_centre_detail_lists_offerings_of_active_tests(
    client: AsyncClient, admin: AuthedUser, catalog: Catalog
) -> None:
    response = await client.get(f"/centres/{catalog.centre.id}")

    tests = {t["code"]: t for t in response.json()["tests"]}
    assert tests["CBC"] == {
        "test_id": str(catalog.test.id),
        "code": "CBC",
        "name": "Complete Blood Count",
        "price": "499.00",
        "is_available": True,
    }
    assert tests["LIPID"]["is_available"] is False
    assert "TSH" not in tests

    await client.delete(f"/tests/{catalog.test.id}", headers=admin.headers)
    after = await client.get(f"/centres/{catalog.centre.id}")
    assert "CBC" not in {t["code"] for t in after.json()["tests"]}


async def test_soft_deleted_centre_is_hidden_from_list(
    client: AsyncClient, admin: AuthedUser, catalog: Catalog
) -> None:
    await client.delete(f"/centres/{catalog.centre.id}", headers=admin.headers)

    body = (await client.get("/centres/")).json()

    assert body["total"] == 0
    assert body["items"] == []


async def test_list_centres_filters(
    client: AsyncClient, admin: AuthedUser, catalog: Catalog
) -> None:
    await client.post("/centres/", json=NEW_CENTRE, headers=admin.headers)

    async def names(**params) -> list[str]:
        response = await client.get("/centres/", params=params)
        assert response.status_code == 200
        return [c["name"] for c in response.json()["items"]]

    assert await names() == ["Apollo Diagnostics", "Metro Labs"]
    assert await names(city="bengaluru") == ["Apollo Diagnostics"]
    assert await names(city="BENGAL") == []  # exact match, not substring
    assert await names(test_id=str(catalog.test.id)) == ["Apollo Diagnostics"]
    assert await names(test_id=str(catalog.unavailable_test.id)) == []
    assert await names(test_id=str(catalog.unoffered_test.id)) == []
    assert await names(city="Kolkata", test_id=str(catalog.test.id)) == []


async def test_list_centres_pagination(client: AsyncClient, admin: AuthedUser) -> None:
    for i in range(3):
        await client.post(
            "/centres/", json={**NEW_CENTRE, "name": f"Lab {i}"}, headers=admin.headers
        )

    body = (await client.get("/centres/", params={"limit": 2, "offset": 2})).json()

    assert body["total"] == 3
    assert (body["limit"], body["offset"]) == (2, 2)
    assert [c["name"] for c in body["items"]] == ["Lab 2"]


async def test_unknown_centre_is_404(client: AsyncClient, admin: AuthedUser) -> None:
    missing = uuid.uuid4()

    assert (await client.get(f"/centres/{missing}")).status_code == 404
    patch = await client.patch(f"/centres/{missing}", json={"city": "X"}, headers=admin.headers)
    assert patch.status_code == 404
    assert (await client.delete(f"/centres/{missing}", headers=admin.headers)).status_code == 404


# --- offerings --------------------------------------------------------------------------------


async def test_upsert_offering_creates_then_updates_price(
    client: AsyncClient, admin: AuthedUser, catalog: Catalog
) -> None:
    url = f"/centres/{catalog.centre.id}/tests/{catalog.unoffered_test.id}"

    created = await client.put(url, json={"price": "350"}, headers=admin.headers)
    assert created.status_code == 200
    assert created.json() == {
        "test_id": str(catalog.unoffered_test.id),
        "code": "TSH",
        "name": "Thyroid Stimulating Hormone",
        "price": "350.00",
        "is_available": True,
    }

    updated = await client.put(url, json={"price": 399.5}, headers=admin.headers)
    assert updated.status_code == 200
    assert updated.json()["price"] == "399.50"

    detail = (await client.get(f"/centres/{catalog.centre.id}")).json()
    assert {t["code"]: t["price"] for t in detail["tests"]}["TSH"] == "399.50"


async def test_upsert_offering_can_make_unavailable_test_available(
    client: AsyncClient, admin: AuthedUser, catalog: Catalog
) -> None:
    url = f"/centres/{catalog.centre.id}/tests/{catalog.unavailable_test.id}"

    await client.put(url, json={"price": "899.00", "is_available": True}, headers=admin.headers)

    listed = await client.get("/centres/", params={"test_id": str(catalog.unavailable_test.id)})
    assert listed.json()["total"] == 1


@pytest.mark.parametrize("price", ["0", "-10", "12.345", "abc", None])
async def test_upsert_offering_rejects_invalid_price(
    client: AsyncClient, admin: AuthedUser, catalog: Catalog, price: str | None
) -> None:
    response = await client.put(
        f"/centres/{catalog.centre.id}/tests/{catalog.test.id}",
        json={"price": price},
        headers=admin.headers,
    )

    assert response.status_code == 422


async def test_upsert_offering_unknown_centre_or_test_is_404(
    client: AsyncClient, admin: AuthedUser, catalog: Catalog
) -> None:
    body = {"price": "100"}
    unknown_centre = await client.put(
        f"/centres/{uuid.uuid4()}/tests/{catalog.test.id}", json=body, headers=admin.headers
    )
    unknown_test = await client.put(
        f"/centres/{catalog.centre.id}/tests/{uuid.uuid4()}", json=body, headers=admin.headers
    )

    assert unknown_centre.status_code == 404
    assert unknown_test.status_code == 404


async def test_remove_offering_marks_it_unavailable(
    client: AsyncClient, admin: AuthedUser, catalog: Catalog
) -> None:
    url = f"/centres/{catalog.centre.id}/tests/{catalog.test.id}"

    response = await client.delete(url, headers=admin.headers)

    assert response.status_code == 204
    detail = (await client.get(f"/centres/{catalog.centre.id}")).json()
    assert {t["code"]: t["is_available"] for t in detail["tests"]}["CBC"] is False
    listed = await client.get("/centres/", params={"test_id": str(catalog.test.id)})
    assert listed.json()["total"] == 0


async def test_remove_unknown_offering_is_404(
    client: AsyncClient, admin: AuthedUser, catalog: Catalog
) -> None:
    url = f"/centres/{catalog.centre.id}/tests/{catalog.unoffered_test.id}"

    assert (await client.delete(url, headers=admin.headers)).status_code == 404


# --- caching ----------------------------------------------------------------------------------


async def test_catalogue_write_invalidates_cached_reads(
    client: AsyncClient, admin: AuthedUser, catalog: Catalog
) -> None:
    centre_url = f"/centres/{catalog.centre.id}"
    assert (await client.get(centre_url)).json()["name"] == "Apollo Diagnostics"  # warm cache
    assert (await client.get("/centres/")).json()["items"][0]["name"] == "Apollo Diagnostics"

    await client.patch(centre_url, json={"name": "Apollo Labs"}, headers=admin.headers)

    assert (await client.get(centre_url)).json()["name"] == "Apollo Labs"
    assert (await client.get("/centres/")).json()["items"][0]["name"] == "Apollo Labs"


async def test_reads_are_served_from_cache(
    client: AsyncClient, db: AsyncSession, catalog: Catalog
) -> None:
    redis_up = False
    with contextlib.suppress(Exception):
        redis_up = await get_redis().ping()
    if not redis_up:
        pytest.skip("Redis is not available; the cache fails open")

    centre_url = f"/centres/{catalog.centre.id}"
    await client.get(centre_url)  # warm cache
    # Change the row behind the API's back: no invalidation happens, so the cached copy wins.
    await db.execute(update(Centre).where(Centre.id == catalog.centre.id).values(name="Sneaky"))
    await db.commit()

    assert (await client.get(centre_url)).json()["name"] == "Apollo Diagnostics"
