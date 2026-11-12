from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated

from pydantic import BaseModel, Field

from app.agent.llm import ToolSpec
from app.schemas.agent import InvoiceProposal
from app.schemas.invoice import InvoiceDraft, LineItem

CENT = Decimal("0.01")
CREATE_INVOICE = "create_invoice"
ASK_CLARIFICATION = "ask_clarification"
DECLINE_REQUEST = "decline_request"


class LineItemInput(BaseModel):
    name: Annotated[
        str, Field(min_length=1, max_length=200, description="What the client is paying for")
    ]
    quantity: Annotated[float, Field(gt=0, description="Number of units, 1 if not stated")] = 1
    unit_amount: Annotated[float, Field(gt=0, description="Price of one unit, as a plain number")]


class CreateInvoiceInput(BaseModel):
    client_name: Annotated[str, Field(min_length=1, max_length=300)]
    client_email: Annotated[
        str,
        Field(description="An email from the message or known clients. Never invent one"),
    ]
    items: Annotated[list[LineItemInput], Field(min_length=1, max_length=20)]
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$", description="ISO currency code")] = "USD"
    due_in_days: Annotated[int, Field(ge=0, le=365, description="Days until payment is due")] = 14
    note: Annotated[
        str | None, Field(max_length=1000, description="Optional note for the client")
    ] = None
    send_immediately: bool = True


class AskClarificationInput(BaseModel):
    question: Annotated[str, Field(min_length=1, max_length=500)]
    missing_fields: Annotated[list[str], Field(min_length=1, max_length=6)]


class DeclineInput(BaseModel):
    reason: Annotated[str, Field(min_length=1, max_length=500)]


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        CREATE_INVOICE,
        "Propose an invoice for a client. Use only when the client's email, at least one item "
        "with a price, and the amounts are all stated or clearly implied. This only proposes; "
        "a person approves before anything is sent.",
        CreateInvoiceInput,
    ),
    ToolSpec(
        ASK_CLARIFICATION,
        "Ask one short question when something needed for an invoice is missing or ambiguous, "
        "such as the client's email address or the price.",
        AskClarificationInput,
    ),
    ToolSpec(
        DECLINE_REQUEST,
        "Politely decline when the message is not a request to create an invoice.",
        DeclineInput,
    ),
)

TOOLS_BY_NAME = {tool.name: tool for tool in TOOLS}


def to_proposal(arguments: CreateInvoiceInput) -> InvoiceProposal:
    items = tuple(
        LineItem(
            name=item.name,
            quantity=Decimal(str(item.quantity)),
            unit_amount=Decimal(str(item.unit_amount)).quantize(CENT, rounding=ROUND_HALF_UP),
        )
        for item in arguments.items
    )
    draft = InvoiceDraft(
        client_name=arguments.client_name,
        client_email=arguments.client_email,
        currency=arguments.currency,
        items=items,
        due_in_days=arguments.due_in_days,
        note=arguments.note,
    )
    return InvoiceProposal(draft=draft, send=arguments.send_immediately)
