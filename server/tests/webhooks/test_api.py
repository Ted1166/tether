from collections.abc import AsyncIterator

import httpx
import pytest

from app.container import Container
from app.db.database import Database
from app.db.repositories import AuditRepository, ClientRepository, InvoiceRepository
from app.main import create_app
from app.paypal.errors import PayPalUnavailable
from app.services.invoice_sync import InvoiceSyncService, SyncSource
from app.services.webhook_service import WebhookService
from tests.db.conftest import StepClock
from tests.webhooks.fakes import (
    INVOICE_ID,
    PAID,
    TODAY,
    FakeInvoices,
    FakeVerifier,
    event_body,
    remote,
    signed_headers,
)

pytestmark = pytest.mark.anyio


class Rig:
    def __init__(self, db: Database, clock: StepClock) -> None:
        self.db = db
        self.invoices = FakeInvoices(**{INVOICE_ID: remote(**PAID)})
        self.verifier = FakeVerifier()
        service = WebhookService(db, self.invoices, self.verifier, today=lambda: TODAY, clock=clock)
        self.container = Container(database=db, webhooks=service)

    def client(self, container: Container | None = None) -> httpx.AsyncClient:
        app = create_app(container or self.container)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://tether")


@pytest.fixture
async def rig(db: Database, clock: StepClock) -> AsyncIterator[Rig]:
    yield Rig(db, clock)


async def test_health_reports_ok(rig: Rig) -> None:
    async with rig.client() as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_a_signed_paid_event_updates_the_ledger(rig: Rig) -> None:
    async with rig.client() as client:
        posted = await client.post(
            "/webhooks/paypal", content=event_body(), headers=signed_headers()
        )
        ledger = await client.get("/invoices")

    assert posted.status_code == 200
    assert posted.json() == {"status": "processed", "event_id": "WH-1"}
    (invoice,) = ledger.json()
    assert invoice["status"] == "paid"
    assert invoice["paypal_id"] == INVOICE_ID
    assert invoice["total"] == "400.00"
    assert invoice["client"]["email"] == "dan@example.com"


async def test_the_exact_request_bytes_reach_the_verifier(rig: Rig) -> None:
    body = event_body()
    async with rig.client() as client:
        await client.post("/webhooks/paypal", content=body, headers=signed_headers())
    assert rig.verifier.calls[0][1] == body


async def test_a_repeated_delivery_is_acknowledged_as_a_duplicate(rig: Rig) -> None:
    async with rig.client() as client:
        await client.post("/webhooks/paypal", content=event_body(), headers=signed_headers())
        again = await client.post(
            "/webhooks/paypal", content=event_body(), headers=signed_headers()
        )
    assert again.status_code == 200
    assert again.json()["status"] == "duplicate"


async def test_a_bad_signature_gets_a_401(rig: Rig) -> None:
    rig.verifier.result = False
    async with rig.client() as client:
        response = await client.post(
            "/webhooks/paypal", content=event_body(), headers=signed_headers()
        )
        ledger = await client.get("/invoices")
    assert response.status_code == 401
    assert ledger.json() == []


async def test_missing_headers_get_a_401(rig: Rig) -> None:
    async with rig.client() as client:
        response = await client.post("/webhooks/paypal", content=event_body())
    assert response.status_code == 401
    assert "missing webhook headers" in response.json()["detail"]


async def test_a_malformed_body_gets_a_401(rig: Rig) -> None:
    async with rig.client() as client:
        response = await client.post(
            "/webhooks/paypal", content=b"not json", headers=signed_headers()
        )
    assert response.status_code == 401


async def test_without_a_webhook_id_the_route_is_unavailable(db: Database) -> None:
    unconfigured = Container(database=db, webhooks=None)
    app = create_app(unconfigured)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://tether") as client:
        response = await client.post(
            "/webhooks/paypal", content=event_body(), headers=signed_headers()
        )
    assert response.status_code == 503
    assert "PAYPAL_WEBHOOK_ID" in response.json()["detail"]


async def test_a_paypal_outage_gets_a_503_so_paypal_retries_later(rig: Rig) -> None:
    rig.invoices.failure = PayPalUnavailable("down", status=503)
    async with rig.client() as client:
        failed = await client.post(
            "/webhooks/paypal", content=event_body(), headers=signed_headers()
        )
        rig.invoices.failure = None
        retried = await client.post(
            "/webhooks/paypal", content=event_body(), headers=signed_headers()
        )
        events = await client.get("/webhook-events")

    assert failed.status_code == 503
    assert retried.status_code == 200
    assert retried.json()["status"] == "processed"
    (event,) = events.json()
    assert event["status"] == "processed"
    assert event["attempts"] == 2


async def test_the_ledger_can_be_filtered_by_status(rig: Rig, clock: StepClock) -> None:
    async with rig.db.session() as session:
        service = InvoiceSyncService(
            InvoiceRepository(session, clock),
            ClientRepository(session, clock),
            AuditRepository(session, clock),
        )
        await service.apply(remote(**PAID), today=TODAY, source=SyncSource.IMPORT)
        await service.apply(remote(id="INV2-OPEN"), today=TODAY, source=SyncSource.IMPORT)

    async with rig.client() as client:
        everything = await client.get("/invoices")
        paid = await client.get("/invoices", params={"status": "paid"})
        sent = await client.get("/invoices", params={"status": "sent"})
        bogus = await client.get("/invoices", params={"status": "nonsense"})

    assert len(everything.json()) == 2
    assert [item["paypal_id"] for item in paid.json()] == [INVOICE_ID]
    assert [item["paypal_id"] for item in sent.json()] == ["INV2-OPEN"]
    assert bogus.status_code == 422


async def test_the_audit_trail_is_readable_over_http(rig: Rig) -> None:
    async with rig.client() as client:
        await client.post("/webhooks/paypal", content=event_body(), headers=signed_headers())
        audit = await client.get("/audit", params={"limit": 5})
    entries = audit.json()
    assert entries[0]["kind"] == "invoice_imported"
    assert entries[0]["actor"] == "paypal"
    assert entries[0]["detail"]["source"] == "webhook"


async def test_the_audit_limit_is_validated(rig: Rig) -> None:
    async with rig.client() as client:
        response = await client.get("/audit", params={"limit": 0})
    assert response.status_code == 422
