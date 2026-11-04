from app.domain.machine import StateMachine
from app.schemas.actions import InvoiceStatus

S = InvoiceStatus

INVOICE_MACHINE = StateMachine[InvoiceStatus](
    "invoice",
    {
        S.DRAFT: frozenset({S.SENT, S.CANCELLED}),
        S.SENT: frozenset({S.PARTIALLY_PAID, S.PAID, S.OVERDUE, S.CANCELLED}),
        S.PARTIALLY_PAID: frozenset({S.PAID, S.OVERDUE, S.CANCELLED}),
        S.OVERDUE: frozenset({S.PARTIALLY_PAID, S.PAID, S.CANCELLED}),
        S.PAID: frozenset(),
        S.CANCELLED: frozenset(),
    },
)


def advance_invoice(current: InvoiceStatus, target: InvoiceStatus) -> InvoiceStatus:
    return INVOICE_MACHINE.advance(current, target)


def is_settled(status: InvoiceStatus) -> bool:
    return INVOICE_MACHINE.is_terminal(status)
