import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app.agent.llm import LLMAuthError, LLMError, LLMUnavailable
from app.agent.loop import AgentOutputError
from app.container import Container
from app.db.repositories import AuditRepository, InvoiceRepository, WebhookEventRepository
from app.paypal.errors import PayPalError
from app.schemas.actions import InvoiceStatus
from app.schemas.agent import AgentReply, ChatRequest, RejectRequest
from app.schemas.policy import Policy
from app.schemas.records import (
    AuditActor,
    AuditRecord,
    InvoiceRecord,
    PendingActionRecord,
    PendingActionStatus,
    WebhookEventRecord,
)
from app.services.actions import (
    ActionBlocked,
    ActionFailed,
    ActionNotDecidable,
    ActionNotFound,
    ActionService,
)
from app.services.policy_service import load_policy, replace_policy
from app.services.webhook_service import InvalidWebhook

logger = logging.getLogger("tether.webhooks")

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
        logger.error("webhook received but PAYPAL_WEBHOOK_ID is not configured")
        raise HTTPException(503, "webhook verification is not configured; set PAYPAL_WEBHOOK_ID")

    body = await request.body()
    try:
        result = await service.handle(body, request.headers)
    except InvalidWebhook as error:
        logger.warning("webhook refused: %s", error)
        raise HTTPException(401, str(error)) from error
    except PayPalError as error:
        logger.error("webhook could not be processed: %s", error)
        raise HTTPException(503, "the event could not be processed right now") from error

    return JSONResponse({"status": result.outcome.value, "event_id": result.event_id})


def _actions(container: Container) -> ActionService:
    if container.actions is None:
        raise HTTPException(503, "actions are not available")
    return container.actions


@router.post("/chat", response_model=AgentReply)
async def chat(body: ChatRequest, container: ContainerDep) -> AgentReply:
    if container.agent is None:
        raise HTTPException(503, "the agent is not configured; set ANTHROPIC_API_KEY")
    try:
        return await container.agent.handle(body.message)
    except LLMAuthError as error:
        logger.error("model key rejected: %s", error)
        raise HTTPException(502, "the model rejected the configured API key") from error
    except LLMUnavailable as error:
        logger.error("model unavailable: %s", error)
        raise HTTPException(503, "the model is unavailable right now") from error
    except (LLMError, AgentOutputError) as error:
        logger.error("agent failed: %s", error)
        raise HTTPException(502, "the agent could not produce a usable answer") from error
    except ActionFailed as error:
        raise HTTPException(502, f"PayPal failed while carrying out the action: {error}") from error


@router.get("/approvals", response_model=list[PendingActionRecord])
async def list_approvals(
    container: ContainerDep,
    status: PendingActionStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[PendingActionRecord]:
    return await _actions(container).list_actions(status, limit)


@router.post("/approvals/{action_id}/approve", response_model=PendingActionRecord)
async def approve_action(action_id: int, container: ContainerDep) -> PendingActionRecord:
    try:
        return await _actions(container).approve(action_id)
    except ActionNotFound as error:
        raise HTTPException(404, str(error)) from error
    except ActionNotDecidable as error:
        raise HTTPException(409, str(error)) from error
    except ActionBlocked as error:
        raise HTTPException(
            409,
            {
                "message": "the policy no longer allows this action",
                "rule": error.decision.rule,
                "reason": error.decision.reason,
            },
        ) from error
    except ActionFailed as error:
        raise HTTPException(502, f"PayPal failed while carrying out the action: {error}") from error


@router.post("/approvals/{action_id}/reject", response_model=PendingActionRecord)
async def reject_action(
    action_id: int, container: ContainerDep, body: RejectRequest | None = None
) -> PendingActionRecord:
    try:
        return await _actions(container).reject(action_id, body.reason if body else None)
    except ActionNotFound as error:
        raise HTTPException(404, str(error)) from error
    except ActionNotDecidable as error:
        raise HTTPException(409, str(error)) from error


@router.get("/policy", response_model=Policy)
async def get_policy(container: ContainerDep) -> Policy:
    async with container.database.session() as session:
        return await load_policy(session)


@router.put("/policy", response_model=Policy)
async def put_policy(policy: Policy, container: ContainerDep) -> Policy:
    async with container.database.session() as session:
        return await replace_policy(session, policy, AuditActor.OWNER)
