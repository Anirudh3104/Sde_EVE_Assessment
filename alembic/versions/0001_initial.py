"""initial schema: users, catalogue, bookings, payments, webhook events

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-02
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

user_role = postgresql.ENUM("PATIENT", "ADMIN", name="user_role", create_type=False)
booking_status = postgresql.ENUM(
    "PENDING", "CONFIRMED", "FAILED", "CANCELLED", name="booking_status", create_type=False
)
payment_status = postgresql.ENUM(
    "PROCESSING", "SUCCESS", "FAILED", name="payment_status", create_type=False
)
refund_status = postgresql.ENUM("REFUND_PENDING", name="refund_status", create_type=False)
webhook_event_result = postgresql.ENUM(
    "APPLIED", "DUPLICATE", "IGNORED", name="webhook_event_result", create_type=False
)
ENUMS = (user_role, booking_status, payment_status, refund_status, webhook_event_result)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
    ]


def upgrade() -> None:
    bind = op.get_bind()
    for enum in ENUMS:
        enum.create(bind, checkfirst=True)

    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("password_hash", sa.String(128), nullable=False),
        sa.Column("full_name", sa.String(200), nullable=False),
        sa.Column("role", user_role, server_default="PATIENT", nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "centres",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("address", sa.String(500), nullable=False),
        sa.Column("city", sa.String(100), nullable=False),
        sa.Column("pincode", sa.String(10), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_centres"),
        sa.UniqueConstraint("name", "city", name="uq_centres_name_city"),
    )
    op.create_index("ix_centres_city", "centres", ["city"])

    op.create_table(
        "diagnostic_tests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_diagnostic_tests"),
        sa.UniqueConstraint("code", name="uq_diagnostic_tests_code"),
    )

    op.create_table(
        "centre_tests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("centre_id", sa.Uuid(), nullable=False),
        sa.Column("test_id", sa.Uuid(), nullable=False),
        sa.Column("price", sa.Numeric(10, 2), nullable=False),
        sa.Column("is_available", sa.Boolean(), server_default="true", nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_centre_tests"),
        sa.ForeignKeyConstraint(["centre_id"], ["centres.id"], ondelete="RESTRICT",
                                name="fk_centre_tests_centre_id_centres"),
        sa.ForeignKeyConstraint(["test_id"], ["diagnostic_tests.id"], ondelete="RESTRICT",
                                name="fk_centre_tests_test_id_diagnostic_tests"),
        sa.UniqueConstraint("centre_id", "test_id", name="uq_centre_tests_centre_test"),
        sa.CheckConstraint("price > 0", name="ck_centre_tests_price_positive"),
    )
    op.create_index("ix_centre_tests_centre_id", "centre_tests", ["centre_id"])
    op.create_index("ix_centre_tests_test_id", "centre_tests", ["test_id"])

    op.create_table(
        "bookings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("centre_id", sa.Uuid(), nullable=False),
        sa.Column("test_id", sa.Uuid(), nullable=False),
        sa.Column("appointment_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("currency", sa.String(3), server_default="INR", nullable=False),
        sa.Column("status", booking_status, server_default="PENDING", nullable=False),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_bookings"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT",
                                name="fk_bookings_user_id_users"),
        sa.ForeignKeyConstraint(["centre_id"], ["centres.id"], ondelete="RESTRICT",
                                name="fk_bookings_centre_id_centres"),
        sa.ForeignKeyConstraint(["test_id"], ["diagnostic_tests.id"], ondelete="RESTRICT",
                                name="fk_bookings_test_id_diagnostic_tests"),
        sa.CheckConstraint("amount > 0", name="ck_bookings_amount_positive"),
    )
    op.create_index("ix_bookings_centre_id", "bookings", ["centre_id"])
    op.create_index("ix_bookings_test_id", "bookings", ["test_id"])
    op.create_index("ix_bookings_status", "bookings", ["status"])
    op.create_index("ix_bookings_user_created", "bookings", ["user_id", "created_at"])
    op.create_index(
        "uq_bookings_active_slot",
        "bookings",
        ["user_id", "centre_id", "test_id", "appointment_at"],
        unique=True,
        postgresql_where=sa.text("status IN ('PENDING', 'CONFIRMED')"),
    )

    op.create_table(
        "payments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("booking_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("currency", sa.String(3), server_default="INR", nullable=False),
        sa.Column("status", payment_status, server_default="PROCESSING", nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("provider_ref", sa.String(64), nullable=False),
        sa.Column("failure_reason", sa.String(255), nullable=True),
        sa.Column("refund_status", refund_status, nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_payments"),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"], ondelete="RESTRICT",
                                name="fk_payments_booking_id_bookings"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT",
                                name="fk_payments_user_id_users"),
        sa.UniqueConstraint("booking_id", name="uq_payments_booking_id"),
        sa.UniqueConstraint("provider_ref", name="uq_payments_provider_ref"),
        sa.UniqueConstraint("user_id", "idempotency_key",
                            name="uq_payments_user_idempotency_key"),
    )
    op.create_index("ix_payments_user_id", "payments", ["user_id"])

    op.create_table(
        "webhook_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.String(64), nullable=False),
        sa.Column("payment_id", sa.Uuid(), nullable=False),
        sa.Column("status", payment_status, nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("signature_valid", sa.Boolean(), nullable=False),
        sa.Column("result", webhook_event_result, nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_webhook_events"),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"], ondelete="RESTRICT",
                                name="fk_webhook_events_payment_id_payments"),
        sa.UniqueConstraint("event_id", name="uq_webhook_events_event_id"),
    )
    op.create_index("ix_webhook_events_payment_id", "webhook_events", ["payment_id"])


def downgrade() -> None:
    for table in ("webhook_events", "payments", "bookings", "centre_tests",
                  "diagnostic_tests", "centres", "users"):
        op.drop_table(table)
    bind = op.get_bind()
    for enum in reversed(ENUMS):
        enum.drop(bind, checkfirst=True)
