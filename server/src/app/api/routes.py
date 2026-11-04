from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app.container import Container
from app.db.repositories import AuditRepository, InvoiceRepository, WebhookEventRepository
from app.paypal.errors import PayPalError
from app.schemas.actions import InvoiceStatus
from app.schemas.records import AuditRecord, InvoiceRecord, WebhookEventRecord
from app.services.webhook_service import InvalidWebhook

router = APIRouter()


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


ContainerDep = Annotated[Container, Depends(get_container)]


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/invoices", response_model=list[InvoiceRecord])
async def list_invoices(
    container: ContainerDep, status: InvoiceStatus | None = None
) -> list[InvoiceRecord]:
    async with container.database.session() as session:
        return await InvoiceRepository(session).list(status)


@router.get("/audit", response_model=list[AuditRecord])
async def list_audit(
    container: ContainerDep,
    invoice_id: int | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[AuditRecord]:
    async with container.database.session() as session:
        return await AuditRepository(session).list(invoice_id=invoice_id, limit=limit)


@router.get("/webhook-events", response_model=list[WebhookEventRecord])
async def list_webhook_events(
    container: ContainerDep, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> list[WebhookEventRecord]:
    async with container.database.session() as session:
        return await WebhookEventRepository(session).list(limit)


@router.post("/webhooks/paypal")
async def paypal_webhook(request: Request, container: ContainerDep) -> JSONResponse:
    service = container.webhooks
    if service is None:
        raise HTTPException(503, "webhook verification is not configured; set PAYPAL_WEBHOOK_ID")

    body = await request.body()
    try:
        result = await service.handle(body, request.headers)
    except InvalidWebhook as error:
        raise HTTPException(401, str(error)) from error
    except PayPalError as error:
        raise HTTPException(503, "the event could not be processed right now") from error

    return JSONResponse({"status": result.outcome.value, "event_id": result.event_id})
