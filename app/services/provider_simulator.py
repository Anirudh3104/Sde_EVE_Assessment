"""A stand-in for a real payment provider.

After POST /payments/ commits, the API schedules `run_payment` as a background task. The
"provider" decides the outcome, waits a little, then delivers a signed webhook to our own
POST /payments/webhook/ exactly as an external provider would: over HTTP, signed with the shared
secret, retried with exponential backoff on network errors/5xx, always with the SAME event_id so
retries are deduplicated by the webhook handler.

`client_factory` and `retry_base_delay_seconds` are module attributes so tests can swap in an
in-process/mock transport and disable the backoff.
"""

import asyncio
import json
import random
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
import structlog

from app.core.config import settings
from app.core.security import sign_webhook_payload
from app.domain.enums import PaymentStatus

logger = structlog.get_logger(__name__)

SIGNATURE_HEADER = "X-Webhook-Signature"
DECLINE_REASON = "Payment declined by provider (simulated)"


def _default_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=5)


client_factory: Callable[[], httpx.AsyncClient] = _default_client
retry_base_delay_seconds: float = 0.5


def decide_outcome(simulate_outcome: Literal["success", "failure"] | None) -> PaymentStatus:
    if simulate_outcome == "success":
        return PaymentStatus.SUCCESS
    if simulate_outcome == "failure":
        return PaymentStatus.FAILED
    succeeded = random.random() < settings.payment_success_rate
    return PaymentStatus.SUCCESS if succeeded else PaymentStatus.FAILED


def build_event(
    payment_id: uuid.UUID, status: PaymentStatus, failure_reason: str | None = None
) -> dict[str, Any]:
    return {
        "event_id": f"evt_{uuid.uuid4().hex}",
        "payment_id": str(payment_id),
        "status": str(status),
        "failure_reason": failure_reason,
        "occurred_at": datetime.now(UTC).isoformat(),
    }


async def run_payment(
    payment_id: uuid.UUID, simulate_outcome: Literal["success", "failure"] | None
) -> None:
    """Background-task entry point. Never raises: a crashed task would only be logged by Starlette.
    If delivery ultimately fails the payment stays PROCESSING (a real system would reconcile)."""
    try:
        status = decide_outcome(simulate_outcome)
        reason = DECLINE_REASON if status == PaymentStatus.FAILED else None
        event = build_event(payment_id, status, reason)
        logger.info("provider_outcome_decided", payment_id=str(payment_id), status=status)
        await deliver(event)
    except Exception:
        logger.exception("provider_simulation_crashed", payment_id=str(payment_id))


async def deliver(event: dict[str, Any]) -> bool:
    """Sign and POST `event` to the webhook URL. Returns True if the webhook accepted it."""
    await asyncio.sleep(settings.provider_delay_seconds)
    # Serialized once: the signature covers these exact bytes, and every retry resends them.
    body = json.dumps(event, separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json", SIGNATURE_HEADER: sign_webhook_payload(body)}
    copies = 2 if settings.simulate_duplicate_webhooks else 1

    delivered = False
    async with client_factory() as client:
        for copy in range(1, copies + 1):
            ok = await _send_with_retries(client, body, headers, event["event_id"], copy)
            delivered = delivered or ok
    return delivered


async def _send_with_retries(
    client: httpx.AsyncClient,
    body: bytes,
    headers: dict[str, str],
    event_id: str,
    copy: int,
) -> bool:
    url = settings.webhook_target_url
    max_attempts = max(1, settings.provider_max_attempts)
    log = logger.bind(event_id=event_id, copy=copy, url=url)

    for attempt in range(1, max_attempts + 1):
        try:
            response = await client.post(url, content=body, headers=headers)
        except httpx.TransportError as exc:
            log.warning("webhook_delivery_error", attempt=attempt, error=repr(exc))
        else:
            if response.is_success:
                log.info("webhook_delivered", attempt=attempt, status=response.status_code)
                return True
            if response.status_code < 500:
                # 4xx: the receiver rejected the event itself; resending identical bytes won't help.
                log.error(
                    "webhook_rejected",
                    attempt=attempt,
                    status=response.status_code,
                    body=response.text[:500],
                )
                return False
            log.warning("webhook_server_error", attempt=attempt, status=response.status_code)

        if attempt < max_attempts:
            await asyncio.sleep(retry_base_delay_seconds * 2 ** (attempt - 1))

    log.error("webhook_delivery_gave_up", attempts=max_attempts)
    return False
