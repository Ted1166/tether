from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pydantic import ValidationError

from app.agent.llm import AnthropicModel
from app.agent.loop import Agent
from app.config import DatabaseSettings, LLMSettings, PayPalSettings
from app.db.database import Database
from app.paypal.client import PayPalClient
from app.paypal.invoices import InvoiceGateway
from app.paypal.webhooks import WebhookVerifier
from app.services.actions import ActionService
from app.services.invoice_executor import PayPalInvoiceExecutor
from app.services.webhook_service import WebhookService


@dataclass
class Container:
    database: Database
    webhooks: WebhookService | None
    actions: ActionService | None = None
    agent: Agent | None = None
    shutdown: Callable[[], Awaitable[None]] | None = None


async def build_container() -> Container:
    paypal = PayPalSettings()  # type: ignore[call-arg]
    database = Database(DatabaseSettings().database_url)
    await database.create_all()

    client = PayPalClient(paypal)
    gateway = InvoiceGateway(client, paypal.merchant_name)
    webhooks = None
    if paypal.webhook_id:
        webhooks = WebhookService(database, gateway, WebhookVerifier(client, paypal.webhook_id))

    actions = ActionService(database, PayPalInvoiceExecutor(gateway), gateway)

    model: AnthropicModel | None = None
    agent: Agent | None = None
    try:
        model = AnthropicModel(LLMSettings())  # type: ignore[call-arg]
        agent = Agent(model, actions)
    except ValidationError:
        agent = None

    async def shutdown() -> None:
        if model is not None:
            await model.aclose()
        await client.aclose()
        await database.dispose()

    return Container(
        database=database, webhooks=webhooks, actions=actions, agent=agent, shutdown=shutdown
    )
