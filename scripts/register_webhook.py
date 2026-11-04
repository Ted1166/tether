import argparse
import asyncio
import sys

from pydantic import ValidationError

from app.config import PayPalSettings
from app.paypal.client import PayPalClient
from app.paypal.errors import PayPalError
from app.paypal.webhooks import INVOICE_EVENT_TYPES, WebhookRegistry


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage the PayPal webhook for Tether.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--url", help="public https URL ending in /webhooks/paypal")
    action.add_argument("--list", action="store_true", help="show registered webhooks")
    action.add_argument("--delete", metavar="WEBHOOK_ID", help="remove a registered webhook")
    return parser.parse_args()


async def run(args: argparse.Namespace) -> None:
    try:
        settings = PayPalSettings()
    except ValidationError:
        print("Missing credentials. Fill in PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET in .env")
        sys.exit(1)

    async with PayPalClient(settings) as client:
        registry = WebhookRegistry(client)

        if args.list:
            hooks = await registry.list()
            if not hooks:
                print("No webhooks are registered for this app.")
            for hook in hooks:
                print(f"{hook.id}  {hook.url}  ({len(hook.event_types)} event types)")
            return

        if args.delete:
            await registry.delete(args.delete)
            print(f"Deleted webhook {args.delete}")
            return

        if not args.url.startswith("https://"):
            print("PayPal only accepts https URLs. Start a tunnel and use its https address.")
            sys.exit(1)

        hook = await registry.register(args.url, INVOICE_EVENT_TYPES)
        print(f"Registered webhook {hook.id}")
        print(f"  url:    {hook.url}")
        print(f"  events: {', '.join(hook.event_types)}")
        print()
        print("Add this line to your .env file, then restart the server:")
        print(f"PAYPAL_WEBHOOK_ID={hook.id}")


def main() -> None:
    try:
        asyncio.run(run(parse_args()))
    except PayPalError as error:
        print(f"PayPal rejected the request: {error}")
        sys.exit(1)


if __name__ == "__main__":
    main()
