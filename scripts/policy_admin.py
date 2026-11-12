import argparse
import asyncio
from decimal import Decimal

from app.config import DatabaseSettings
from app.db.database import Database
from app.schemas.policy import AmountPolicy, ContactMode, ContactPolicy, Policy
from app.schemas.records import AuditActor
from app.services.policy_service import load_policy, replace_policy


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect and change the Tether policy.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("show")
    allow = commands.add_parser("allow", help="add a client email to the approved contacts")
    allow.add_argument("email")
    limits = commands.add_parser("limits", help="change the money limits")
    limits.add_argument("--auto-approve-below", type=Decimal)
    limits.add_argument("--hard-cap", type=Decimal)
    mode = commands.add_parser("contacts", help="choose who the agent may contact")
    mode.add_argument("mode", choices=[item.value for item in ContactMode])
    return parser.parse_args()


def changed(policy: Policy, args: argparse.Namespace) -> Policy:
    if args.command == "allow":
        allowed = policy.contacts.allowed | {args.email}
        return policy.model_copy(
            update={
                "contacts": ContactPolicy(
                    mode=policy.contacts.mode, allowed=allowed, blocked=policy.contacts.blocked
                )
            }
        )
    if args.command == "limits":
        amounts = AmountPolicy(
            auto_approve_below=args.auto_approve_below or policy.amounts.auto_approve_below,
            hard_cap=args.hard_cap or policy.amounts.hard_cap,
        )
        return policy.model_copy(update={"amounts": amounts})
    contacts = ContactPolicy(
        mode=ContactMode(args.mode),
        allowed=policy.contacts.allowed,
        blocked=policy.contacts.blocked,
    )
    return policy.model_copy(update={"contacts": contacts})


async def run(args: argparse.Namespace) -> None:
    database = Database(DatabaseSettings().database_url)
    await database.create_all()
    async with database.session() as session:
        policy = await load_policy(session)
        if args.command != "show":
            policy = await replace_policy(session, changed(policy, args), AuditActor.OWNER)
    print(policy.model_dump_json(indent=2))
    await database.dispose()


def main() -> None:
    asyncio.run(run(parse_args()))


if __name__ == "__main__":
    main()
