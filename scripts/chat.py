import asyncio
import sys

from app.container import build_container
from app.paypal.errors import PayPalError
from app.schemas.records import PendingActionRecord, PendingActionStatus
from app.services.actions import ActionBlocked, ActionError


def show(record: PendingActionRecord) -> None:
    print(f"  action {record.id} [{record.status.value}] {record.summary}")
    print(f"  policy: {record.verdict.value} ({record.rule}) {record.reason}")
    if record.result.get("number"):
        print(f"  invoice: {record.result['number']} ({record.result.get('paypal_id')})")
    if record.error:
        print(f"  note: {record.error}")


async def read_line() -> str:
    return await asyncio.get_running_loop().run_in_executor(None, input, "you> ")


async def run() -> None:
    container = await build_container()
    if container.agent is None or container.actions is None:
        print("Set ANTHROPIC_API_KEY in .env to talk to the agent.")
        await container.shutdown() if container.shutdown else None
        sys.exit(1)

    print("Tether. Describe an invoice, or use: pending, approve N, reject N, quit\n")
    try:
        while True:
            try:
                line = (await read_line()).strip()
            except EOFError:
                break
            if not line:
                continue
            command, _, rest = line.partition(" ")

            try:
                if command == "quit":
                    break
                if command == "pending":
                    for record in await container.actions.list_actions(PendingActionStatus.PENDING):
                        show(record)
                elif command in ("approve", "reject") and rest.strip().isdigit():
                    action_id = int(rest.strip())
                    if command == "approve":
                        show(await container.actions.approve(action_id))
                    else:
                        show(await container.actions.reject(action_id))
                else:
                    reply = await container.agent.handle(line)
                    print(f"tether> {reply.message}")
                    if reply.action is not None:
                        show(reply.action)
            except ActionBlocked as error:
                print(f"blocked: {error.decision.reason}")
            except (ActionError, PayPalError) as error:
                print(f"error: {error}")
    finally:
        if container.shutdown is not None:
            await container.shutdown()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
