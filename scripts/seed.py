"""Seed demo data: a few centres, a test catalogue with per-centre prices, and an admin user.

Idempotent: safe to run more than once.

    python -m scripts.seed
"""

import asyncio
from decimal import Decimal

from sqlalchemy import select

from app.core.security import hash_password
from app.db.session import SessionLocal
from app.domain.enums import UserRole
from app.models import Centre, CentreTest, DiagnosticTest, User

ADMIN_EMAIL = "admin@eve.health"
ADMIN_PASSWORD = "Admin@12345"

TESTS = [
    ("CBC", "Complete Blood Count", "Measures red cells, white cells and platelets."),
    ("LIPID", "Lipid Profile", "Cholesterol and triglyceride levels."),
    ("TSH", "Thyroid Stimulating Hormone", "Screens thyroid function."),
    ("HBA1C", "HbA1c", "Average blood sugar over ~3 months."),
    ("VITD", "Vitamin D (25-OH)", "Vitamin D level."),
]

# (name, address, city, pincode, {test_code: price})
CENTRES = [
    (
        "EVE Diagnostics Indiranagar",
        "100 Feet Rd, Indiranagar",
        "Bengaluru",
        "560038",
        {"CBC": "399", "LIPID": "799", "TSH": "449", "HBA1C": "549"},
    ),
    (
        "EVE Diagnostics Koramangala",
        "80 Feet Rd, Koramangala",
        "Bengaluru",
        "560034",
        {"CBC": "449", "LIPID": "849", "VITD": "1199"},
    ),
    (
        "EVE Diagnostics Banjara Hills",
        "Road No. 12, Banjara Hills",
        "Hyderabad",
        "500034",
        {"CBC": "349", "TSH": "399", "HBA1C": "499", "VITD": "999"},
    ),
]


async def seed() -> None:
    async with SessionLocal() as db:
        if not await db.scalar(select(User).where(User.email == ADMIN_EMAIL)):
            db.add(
                User(
                    email=ADMIN_EMAIL,
                    password_hash=hash_password(ADMIN_PASSWORD),
                    full_name="EVE Admin",
                    role=UserRole.ADMIN,
                )
            )

        tests: dict[str, DiagnosticTest] = {}
        for code, name, description in TESTS:
            test = await db.scalar(select(DiagnosticTest).where(DiagnosticTest.code == code))
            if test is None:
                test = DiagnosticTest(code=code, name=name, description=description)
                db.add(test)
            tests[code] = test
        await db.flush()

        for name, address, city, pincode, prices in CENTRES:
            centre = await db.scalar(select(Centre).where(Centre.name == name, Centre.city == city))
            if centre is None:
                centre = Centre(name=name, address=address, city=city, pincode=pincode)
                db.add(centre)
                await db.flush()
            for code, price in prices.items():
                exists = await db.scalar(
                    select(CentreTest).where(
                        CentreTest.centre_id == centre.id, CentreTest.test_id == tests[code].id
                    )
                )
                if exists is None:
                    db.add(
                        CentreTest(
                            centre_id=centre.id, test_id=tests[code].id, price=Decimal(price)
                        )
                    )
        await db.commit()

    print(f"Seed complete. Admin login: {ADMIN_EMAIL} / {ADMIN_PASSWORD}")


if __name__ == "__main__":
    asyncio.run(seed())
