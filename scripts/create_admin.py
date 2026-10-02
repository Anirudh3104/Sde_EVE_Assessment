"""Create an admin user, or promote an existing user to admin.

Admins can't be created through the public signup API by design.

    python -m scripts.create_admin admin2@example.com "S3cure-pass" "Ops Admin"
"""

import argparse
import asyncio

from sqlalchemy import select

from app.core.security import hash_password
from app.db.session import SessionLocal
from app.domain.enums import UserRole
from app.models import User


async def create_admin(email: str, password: str, full_name: str) -> None:
    email = email.strip().lower()
    async with SessionLocal() as db:
        user = await db.scalar(select(User).where(User.email == email))
        if user is None:
            db.add(
                User(
                    email=email,
                    password_hash=hash_password(password),
                    full_name=full_name,
                    role=UserRole.ADMIN,
                )
            )
            action = "Created"
        else:
            user.role = UserRole.ADMIN
            action = "Promoted"
        await db.commit()
    print(f"{action} admin {email}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email")
    parser.add_argument("password")
    parser.add_argument("full_name", nargs="?", default="Admin")
    args = parser.parse_args()
    if len(args.password) < 8:
        parser.error("password must be at least 8 characters")
    asyncio.run(create_admin(args.email, args.password, args.full_name))


if __name__ == "__main__":
    main()
