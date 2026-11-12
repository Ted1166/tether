import copy
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from app.agent.llm import ToolCall, ToolSpec
from app.db.database import Database
from app.db.repositories import PolicyRepository
from app.paypal.mappers import from_paypal_invoice
from app.schemas.actions import ActionKind
from app.schemas.agent import InvoiceProposal
from app.schemas.invoice import InvoiceDraft, LineItem, RemoteInvoice
from app.schemas.policy import ContactPolicy, Policy
from app.services.actions import ActionService
from tests.db.conftest import StepClock
from tests.paypal.helpers import sample_invoice

TODAY = date(2026, 11, 4)
DAN = "dan@example.com"


def base_policy(**changes: Any) -> Policy:
    policy = Policy(contacts=ContactPolicy(allowed=frozenset({DAN, "maya@example.com"})))
    return policy.model_copy(update=changes) if changes else policy


def trusting_policy(**changes: Any) -> Policy:
    return base_policy(
        auto_actions=frozenset({ActionKind.CREATE_INVOICE, ActionKind.SEND_INVOICE}), **changes
    )


async def set_policy(db: Database, policy: Policy) -> Policy:
    async with db.session() as session:
        return await PolicyRepository(session).save(policy)


def proposal(
    amount: str = "400", email: str = DAN, name: str = "Dan Okoro", send: bool = True
) -> InvoiceProposal:
    draft = InvoiceDraft(
        client_name=name,
        client_email=email,
        items=(LineItem(name="Logo design", unit_amount=Decimal(amount)),),
        due_in_days=14,
    )
    return InvoiceProposal(draft=draft, send=send)


def tool_call(name: str, call_id: str = "toolu_1", **arguments: Any) -> ToolCall:
    block = {"type": "tool_use", "id": call_id, "name": name, "input": arguments}
    return ToolCall(id=call_id, name=name, input=arguments, content=(block,))


def invoice_call(**overrides: Any) -> ToolCall:
    arguments: dict[str, Any] = {
        "client_name": "Dan Okoro",
        "client_email": DAN,
        "items": [{"name": "Logo design", "unit_amount": 400}],
        "due_in_days": 14,
    }
    arguments.update(overrides)
    return tool_call("create_invoice", **arguments)


class FakeModel:
    def __init__(self, *script: ToolCall | Exception) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    async def call_tool(
        self,
        *,
        system: str,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[ToolSpec],
        force: str | None = None,
        max_tokens: int = 1024,
    ) -> ToolCall:
        self.requests.append(
            {
                "system": system,
                "messages": copy.deepcopy(list(messages)),
                "tools": [tool.name for tool in tools],
                "force": force,
            }
        )
        if not self.script:
            raise AssertionError("the fake model ran out of scripted answers")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeExecutor:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.created: dict[str, tuple[InvoiceDraft, bool]] = {}
        self.create_count = 0
        self.failures: list[tuple[str, Exception]] = []

    def fail_next(self, stage: str, error: Exception) -> None:
        self.failures.append((stage, error))

    def _maybe_fail(self, stage: str) -> None:
        for index, (target, error) in enumerate(self.failures):
            if target == stage:
                self.failures.pop(index)
                raise error

    async def create_and_send(
        self,
        draft: InvoiceDraft,
        *,
        send: bool,
        action_id: int,
        resume_paypal_id: str | None,
        on_created: Callable[[str], Awaitable[None]],
    ) -> str:
        self.calls.append(
            {"action_id": action_id, "send": send, "resume": resume_paypal_id, "draft": draft}
        )
        paypal_id = resume_paypal_id
        if paypal_id is None:
            self._maybe_fail("create")
            self.create_count += 1
            paypal_id = f"INV2-FAKE-{action_id}-{self.create_count}"
            self.created[paypal_id] = (draft, False)
            await on_created(paypal_id)
        if send:
            self._maybe_fail("send")
            self.created[paypal_id] = (draft, True)
        return paypal_id


class EchoInvoices:
    def __init__(self, executor: FakeExecutor) -> None:
        self.executor = executor
        self.requested: list[str] = []

    async def get(self, invoice_id: str) -> RemoteInvoice:
        self.requested.append(invoice_id)
        draft, sent = self.executor.created[invoice_id]
        position = list(self.executor.created).index(invoice_id) + 1
        value = {"currency_code": draft.currency, "value": f"{draft.total:.2f}"}
        first, _, rest = draft.client_name.partition(" ")
        name = {"given_name": first, **({"surname": rest} if rest else {})}
        payload = sample_invoice(
            id=invoice_id,
            status="SENT" if sent else "DRAFT",
            amount=value,
            due_amount=value,
            primary_recipients=[
                {"billing_info": {"name": name, "email_address": draft.client_email}}
            ],
        )
        payload["detail"] = {
            **payload["detail"],
            "invoice_number": f"{position:04d}",
            "currency_code": draft.currency,
            "reference": draft.reference,
        }
        return from_paypal_invoice(payload)


class Rig:
    def __init__(self, db: Database, clock: StepClock) -> None:
        self.db = db
        self.clock = clock
        self.executor = FakeExecutor()
        self.invoices = EchoInvoices(self.executor)
        self.service = ActionService(
            db, self.executor, self.invoices, today=lambda: TODAY, clock=clock
        )
