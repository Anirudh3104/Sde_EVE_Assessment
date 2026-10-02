"""Import every model here so `Base.metadata` is complete for Alembic and tests."""

from app.models.booking import Booking
from app.models.catalog import Centre, CentreTest, DiagnosticTest
from app.models.payment import Payment, WebhookEvent
from app.models.user import User

__all__ = ["Booking", "Centre", "CentreTest", "DiagnosticTest", "Payment", "User", "WebhookEvent"]
