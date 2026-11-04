import argparse
import asyncio
import sys
import uuid
from datetime import date
from decimal import Decimal

from pydantic import ValidationError

from app.config import PayPalSettings
from app.paypal.client import PayPalClient
from app.paypal.errors import PayPalError
from app.paypal.invoices import InvoiceGateway
from app.paypal.mappers import to_internal_status
from app.schemas.invoice import InvoiceDraft, LineItem, RemoteInvoice


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create, send and settle one PayPal sandbox invoice."
    )
    parser.add_argument(
        "--client-email", required=True, help="email of your sandbox personal account"
    )
    parser.add_argument("--client-name", default="Sandbox Buyer")
    parser.add_argument("--amount", default="40.00")
    parser.add_argument("--due-in-days", type=int, default=14)
    parser.add_argument("--remind", action="store_true", help="send a reminder after sending")
    parser.add_argument(
        "--record-payment",
        action="store_true",
        help="record the payment through the API instead of paying from the buyer account",
    )
    return parser.parse_args()


def show(label: str, invoice: RemoteInvoice, today: date) -> None:
    internal = to_internal_status(invoice, today)
    print(f"{label}")
    print(f"  id:        {invoice.id}")
    print(f"  number:    {invoice.number}")
    print(f"  paypal:    {invoice.paypal_status.value}")
    print(f"  tether:    {internal.value}")
    print(f"  total:     {invoice.currency} {invoice.total}")
    print(f"  paid:      {invoice.currency} {invoice.paid}")
    print(f"  balance:   {invoice.currency} {invoice.balance}")
    print(f"  due:       {invoice.due_date}")
    print(f"  payer url: {invoice.payer_view_url}")
    print()


async def run(args: argparse.Namespace) -> None:
    try:
        settings = PayPalSettings()
    except ValidationError:
        print("Missing credentials. Copy .env.example to .env and fill in PAYPAL_CLIENT_ID")
        print("and PAYPAL_CLIENT_SECRET from your sandbox app in the PayPal developer dashboard.")
        sys.exit(1)

    today = date.today()
    amount = Decimal(args.amount)
    draft = InvoiceDraft(
        client_name=args.client_name,
        client_email=args.client_email,
        items=(LineItem(name="Tether smoke test", unit_amount=amount),),
        due_in_days=args.due_in_days,
        note="Created by the Tether smoke test.",
        reference=f"tether-smoke-{uuid.uuid4().hex[:8]}",
    )

    async with PayPalClient(settings) as client:
        gateway = InvoiceGateway(client, merchant_name=settings.merchant_name)

        print("Creating draft invoice")
        invoice_id = await gateway.create_draft(draft, issue_date=today)
        show("Draft created", await gateway.get(invoice_id), today)

        print("Sending invoice")
        await gateway.send(invoice_id)
        sent = await gateway.get(invoice_id)
        show("Invoice sent", sent, today)

        if args.remind:
            print("Sending reminder")
            await gateway.remind(
                invoice_id,
                subject="A quick reminder about your invoice",
                note="Just a friendly nudge from the Tether smoke test.",
            )
            print("Reminder sent\n")

        if args.record_payment:
            print("Recording payment through the API")
            payment_id = await gateway.record_payment(
                invoice_id, amount=amount, currency=draft.currency, paid_on=today
            )
            print(f"Payment recorded: {payment_id}\n")
        else:
            print("Open the payer url above, sign in with your sandbox personal account,")
            print("and pay the invoice. Press Enter here when you are done.")
            await asyncio.get_running_loop().run_in_executor(None, input)

        show("Final state", await gateway.get(invoice_id), today)


def main() -> None:
    try:
        asyncio.run(run(parse_args()))
    except PayPalError as error:
        print(f"PayPal rejected the request: {error}")
        sys.exit(1)


if __name__ == "__main__":
    main()
