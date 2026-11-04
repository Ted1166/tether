from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.config import DatabaseSettings, PayPalSettings
from app.db.database import Database
from app.paypal.client import PayPalClient
from app.paypal.invoices import InvoiceGateway
from app.paypal.webhooks import WebhookVerifier
from app.services.webhook_service import WebhookService


@dataclass
class Container:
    database: Database
    webhooks: WebhookService | None
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

    async def shutdown() -> None:
        await client.aclose()
        await database.dispose()

    return Container(database=database, webhooks=webhooks, shutdown=shutdown)
