import json
import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Header, Request, Response, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError as PydanticValidationError

from app.core.config import settings
from app.core.deps import CurrentUser, DbSession
from app.core.errors import UnauthorizedError
from app.core.rate_limit import limiter, user_or_ip_key
from app.core.security import verify_webhook_signature
from app.schemas.common import ErrorResponse
from app.schemas.payment import PaymentCreate, PaymentOut, WebhookAck, WebhookEventIn
from app.services import payment as payment_service
from app.services import provider_simulator
from app.services import webhook as webhook_service

router = APIRouter(prefix="/payments", tags=["payments"])

IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=255,
        description="Client-generated key; retrying with the same key and body is safe.",
    ),
]


@router.post(
    "/",
    response_model=PaymentOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Initiate payment for a PENDING booking",
    responses={
        200: {"model": PaymentOut, "description": "Idempotent replay of an earlier request"},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
)
@limiter.limit(lambda: settings.payment_rate_limit, key_func=user_or_ip_key)
async def create_payment(
    request: Request,
    response: Response,
    data: PaymentCreate,
    idempotency_key: IdempotencyKey,
    user: CurrentUser,
    db: DbSession,
    background_tasks: BackgroundTasks,
) -> PaymentOut:
    """Creates a PROCESSING payment and hands it to the (simulated) provider, which reports the
    outcome asynchronously via the webhook. Returns 202; poll GET /payments/{id} for the result.
    """
    result = await payment_service.create_payment(db, user, data, idempotency_key)
    if result.replayed:
        response.status_code = status.HTTP_200_OK
        response.headers["Idempotent-Replayed"] = "true"
    else:
        background_tasks.add_task(
            provider_simulator.run_payment, result.payment.id, data.simulate_outcome
        )
    return PaymentOut.model_validate(result.payment)


# Authenticated by HMAC signature instead of JWT, and deliberately not rate-limited: providers
# retry aggressively and dropping their deliveries would lose payment outcomes.
@router.post(
    "/webhook/",
    response_model=WebhookAck,
    summary="Payment provider webhook (HMAC-signed, idempotent)",
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": WebhookEventIn.model_json_schema()}},
        },
        "parameters": [
            {
                "name": "X-Webhook-Signature",
                "in": "header",
                "required": True,
                "description": "Hex HMAC-SHA256 of the raw request body using the shared secret.",
                "schema": {"type": "string"},
            }
        ],
    },
)
async def payment_webhook(request: Request, db: DbSession) -> WebhookAck:
    """Applies a provider event. Re-delivering the same event_id is a no-op that still returns
    200 (`result: DUPLICATE`) so the provider stops retrying."""
    raw = await request.body()
    # The signature covers the exact bytes sent, so verify before parsing anything.
    if not verify_webhook_signature(raw, request.headers.get(provider_simulator.SIGNATURE_HEADER)):
        raise UnauthorizedError("Invalid webhook signature", code="invalid_signature")
    try:
        event = WebhookEventIn.model_validate_json(raw)
    except PydanticValidationError as exc:
        errors = [{**err, "loc": ("body", *err["loc"])} for err in exc.errors()]
        raise RequestValidationError(errors) from exc
    return await webhook_service.process_event(db, event, payload=json.loads(raw))


@router.get(
    "/{payment_id}",
    response_model=PaymentOut,
    summary="Get a payment (owner or admin)",
    responses={404: {"model": ErrorResponse}},
)
async def get_payment(payment_id: uuid.UUID, user: CurrentUser, db: DbSession) -> PaymentOut:
    payment = await payment_service.get_payment(db, user, payment_id)
    return PaymentOut.model_validate(payment)
