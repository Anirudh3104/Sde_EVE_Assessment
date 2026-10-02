"""The simulated provider's outcome selection, event format and delivery/retry policy.

Delivery goes to an httpx.MockTransport, so no app or database is involved.
"""

import json
import uuid

import httpx
import pytest

from app.core.config import settings
from app.core.security import verify_webhook_signature
from app.domain.enums import PaymentStatus
from app.schemas.payment import WebhookEventIn
from app.services import provider_simulator
from app.services.provider_simulator import SIGNATURE_HEADER, build_event, decide_outcome, deliver


@pytest.fixture(autouse=True)
def fast_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "provider_delay_seconds", 0)
    monkeypatch.setattr(settings, "provider_max_attempts", 3)
    monkeypatch.setattr(settings, "simulate_duplicate_webhooks", False)
    monkeypatch.setattr(settings, "webhook_target_url", "http://provider.test/payments/webhook/")
    monkeypatch.setattr(provider_simulator, "retry_base_delay_seconds", 0)


def use_transport(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:
    """Route the provider's HTTP calls to `handler`; returns the list of requests it sent."""
    sent: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return handler(request, len(sent))

    monkeypatch.setattr(
        provider_simulator,
        "client_factory",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(recording)),
    )
    return sent


def test_forced_outcomes() -> None:
    assert decide_outcome("success") == PaymentStatus.SUCCESS
    assert decide_outcome("failure") == PaymentStatus.FAILED


@pytest.mark.parametrize(
    ("rate", "expected"), [(1.0, PaymentStatus.SUCCESS), (0.0, PaymentStatus.FAILED)]
)
def test_random_outcome_follows_success_rate(
    monkeypatch: pytest.MonkeyPatch, rate: float, expected: PaymentStatus
) -> None:
    monkeypatch.setattr(settings, "payment_success_rate", rate)
    assert {decide_outcome(None) for _ in range(20)} == {expected}


def test_event_matches_webhook_contract() -> None:
    payment_id = uuid.uuid4()
    event = build_event(payment_id, PaymentStatus.FAILED, "declined")

    parsed = WebhookEventIn.model_validate(event)

    assert parsed.payment_id == payment_id
    assert parsed.status == "FAILED"
    assert parsed.failure_reason == "declined"
    assert event["event_id"].startswith("evt_")
    assert build_event(payment_id, PaymentStatus.FAILED)["event_id"] != event["event_id"]


async def test_delivery_is_signed(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = use_transport(monkeypatch, lambda req, n: httpx.Response(200))
    event = build_event(uuid.uuid4(), PaymentStatus.SUCCESS)

    assert await deliver(event) is True

    (request,) = sent
    assert str(request.url) == settings.webhook_target_url
    assert verify_webhook_signature(request.content, request.headers[SIGNATURE_HEADER])
    assert json.loads(request.content) == event


async def test_retries_network_errors_and_5xx_with_the_same_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request, attempt: int) -> httpx.Response:
        if attempt == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(502 if attempt == 2 else 200)

    sent = use_transport(monkeypatch, handler)

    assert await deliver(build_event(uuid.uuid4(), PaymentStatus.SUCCESS)) is True
    assert len(sent) == 3
    assert len({r.content for r in sent}) == 1
    assert len({r.headers[SIGNATURE_HEADER] for r in sent}) == 1


async def test_gives_up_after_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = use_transport(monkeypatch, lambda req, n: httpx.Response(503))

    assert await deliver(build_event(uuid.uuid4(), PaymentStatus.SUCCESS)) is False
    assert len(sent) == settings.provider_max_attempts


@pytest.mark.parametrize("status_code", [400, 401, 404, 422])
async def test_4xx_is_terminal(monkeypatch: pytest.MonkeyPatch, status_code: int) -> None:
    sent = use_transport(monkeypatch, lambda req, n: httpx.Response(status_code))

    assert await deliver(build_event(uuid.uuid4(), PaymentStatus.SUCCESS)) is False
    assert len(sent) == 1


async def test_duplicate_simulation_resends_identical_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "simulate_duplicate_webhooks", True)
    sent = use_transport(monkeypatch, lambda req, n: httpx.Response(200))

    assert await deliver(build_event(uuid.uuid4(), PaymentStatus.SUCCESS)) is True
    assert len(sent) == 2
    assert sent[0].content == sent[1].content


async def test_run_payment_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(request: httpx.Request, attempt: int) -> httpx.Response:
        raise RuntimeError("boom")

    use_transport(monkeypatch, broken)

    await provider_simulator.run_payment(uuid.uuid4(), "success")  # logged, not raised
