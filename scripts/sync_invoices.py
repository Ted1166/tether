import argparse
import asyncio
import sys
from datetime import date

from pydantic import ValidationError

from app.config import DatabaseSettings, PayPalSettings
from app.db.database import Database
from app.db.repositories import AuditRepository, ClientRepository, InvoiceRepository
from app.paypal.client import PayPalClient
from app.paypal.errors import PayPalError
from app.paypal.invoices import InvoiceGateway
from app.schemas.invoice import RemoteInvoice
from app.services.invoice_sync import InvoiceSyncService, SyncSource

MAX_PAGES = 20


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pull your PayPal sandbox invoices into Tether.")
    parser.add_argument("--reset", action="store_true", help="wipe the local database first")
    return parser.parse_args()


async def fetch_all(gateway: InvoiceGateway) -> list[RemoteInvoice]:
    collected: list[RemoteInvoice] = []
    page = 1
    while page <= MAX_PAGES:
        result = await gateway.list_invoices(page=page, page_size=100)
        collected.extend(result.items)
        if page >= result.total_pages:
            break
        page += 1
    return collected


def row(*cells: str) -> str:
    widths = (8, 22, 15, 15, 12, 12, 12, 10)
    return "  ".join(cell[:width].ljust(width) for cell, width in zip(cells, widths, strict=False))


async def run(args: argparse.Namespace) -> None:
    try:
        paypal = PayPalSettings()
    except ValidationError:
        print("Missing credentials. Fill in PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET in .env")
        sys.exit(1)

    database = Database(DatabaseSettings().database_url)
    if args.reset:
        await database.drop_all()
    await database.create_all()
    today = date.today()

    async with PayPalClient(paypal) as client:
        remote = await fetch_all(InvoiceGateway(client, paypal.merchant_name))
    print(f"Fetched {len(remote)} invoice(s) from PayPal\n")

    results = {}
    async with database.session() as session:
        service = InvoiceSyncService(
            InvoiceRepository(session), ClientRepository(session), AuditRepository(session)
        )
        for invoice in remote:
            results[invoice.id] = await service.apply(
                invoice, today=today, source=SyncSource.IMPORT
            )

    async with database.session() as session:
        ledger = await InvoiceRepository(session).list()
        audit = await AuditRepository(session).list(limit=1000)

    print(
        row(
            "Number",
            "Client",
            "Tether status",
            "PayPal status",
            "Total",
            "Balance",
            "Due",
            "Result",
        )
    )
    for record in ledger:
        outcome = results[record.paypal_id].outcome.value if record.paypal_id in results else "-"
        client_name = record.client.name if record.client else "-"
        print(
            row(
                record.number or "-",
                client_name,
                record.status.value,
                record.paypal_status.value,
                f"{record.currency} {record.total}",
                f"{record.currency} {record.balance}",
                str(record.due_date or "-"),
                outcome,
            )
        )
    print(f"\n{len(ledger)} invoice(s) and {len(audit)} audit entries in the local database")
    await database.dispose()


def main() -> None:
    try:
        asyncio.run(run(parse_args()))
    except PayPalError as error:
        print(f"PayPal rejected the request: {error}")
        sys.exit(1)


if __name__ == "__main__":
    main()
