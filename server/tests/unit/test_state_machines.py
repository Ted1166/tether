import pytest

from app.domain.errors import IllegalTransition
from app.domain.invoice_state import INVOICE_MACHINE, advance_invoice, is_settled
from app.domain.machine import StateMachine
from app.domain.negotiation_state import (
    NEGOTIATION_MACHINE,
    NegotiationRoute,
    NegotiationState,
    advance_negotiation,
    route_for,
)
from app.schemas.actions import InvoiceStatus
from app.schemas.negotiation import ClientIntent

Inv = InvoiceStatus
N = NegotiationState


def reachable[S: (InvoiceStatus, NegotiationState)](machine: StateMachine[S], start: S) -> set[S]:
    seen = {start}
    frontier = [start]
    while frontier:
        for state in machine.next_states(frontier.pop()):
            if state not in seen:
                seen.add(state)
                frontier.append(state)
    return seen


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (Inv.DRAFT, Inv.SENT),
        (Inv.DRAFT, Inv.CANCELLED),
        (Inv.SENT, Inv.PARTIALLY_PAID),
        (Inv.SENT, Inv.PAID),
        (Inv.SENT, Inv.OVERDUE),
        (Inv.PARTIALLY_PAID, Inv.PAID),
        (Inv.PARTIALLY_PAID, Inv.OVERDUE),
        (Inv.OVERDUE, Inv.PARTIALLY_PAID),
        (Inv.OVERDUE, Inv.PAID),
        (Inv.OVERDUE, Inv.CANCELLED),
    ],
)
def test_legal_invoice_transitions(current: Inv, target: Inv) -> None:
    assert advance_invoice(current, target) == target


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (Inv.PAID, Inv.SENT),
        (Inv.PAID, Inv.PARTIALLY_PAID),
        (Inv.PAID, Inv.OVERDUE),
        (Inv.PAID, Inv.CANCELLED),
        (Inv.CANCELLED, Inv.SENT),
        (Inv.CANCELLED, Inv.PAID),
        (Inv.DRAFT, Inv.PAID),
        (Inv.DRAFT, Inv.OVERDUE),
        (Inv.SENT, Inv.DRAFT),
        (Inv.OVERDUE, Inv.SENT),
    ],
)
def test_illegal_invoice_transitions_raise(current: Inv, target: Inv) -> None:
    with pytest.raises(IllegalTransition) as caught:
        advance_invoice(current, target)
    assert caught.value.machine == "invoice"
    assert caught.value.current == current.value
    assert caught.value.target == target.value


@pytest.mark.parametrize("status", list(InvoiceStatus))
def test_repeating_the_current_invoice_status_is_harmless(status: Inv) -> None:
    assert advance_invoice(status, status) == status


def test_a_late_overdue_event_cannot_undo_a_payment() -> None:
    with pytest.raises(IllegalTransition):
        advance_invoice(Inv.PAID, Inv.OVERDUE)


def test_only_paid_and_cancelled_invoices_are_settled() -> None:
    assert {status for status in InvoiceStatus if is_settled(status)} == {Inv.PAID, Inv.CANCELLED}


def test_every_invoice_status_has_a_defined_row() -> None:
    assert INVOICE_MACHINE.states == frozenset(InvoiceStatus)


def test_every_invoice_state_can_reach_a_settled_state() -> None:
    for status in InvoiceStatus:
        assert any(is_settled(state) for state in reachable(INVOICE_MACHINE, status))


def test_a_new_invoice_can_follow_the_whole_lifecycle() -> None:
    assert reachable(INVOICE_MACHINE, Inv.DRAFT) == set(InvoiceStatus)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (N.REQUESTED, N.AWAITING_APPROVAL),
        (N.REQUESTED, N.ESCALATED),
        (N.AWAITING_APPROVAL, N.OFFERED),
        (N.AWAITING_APPROVAL, N.EXPIRED),
        (N.OFFERED, N.ACCEPTED),
        (N.OFFERED, N.DECLINED),
        (N.OFFERED, N.EXPIRED),
        (N.ACCEPTED, N.COMPLETED),
        (N.DECLINED, N.ESCALATED),
    ],
)
def test_legal_negotiation_transitions(current: N, target: N) -> None:
    assert advance_negotiation(current, target) == target


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (N.REQUESTED, N.OFFERED),
        (N.REQUESTED, N.ACCEPTED),
        (N.AWAITING_APPROVAL, N.ACCEPTED),
        (N.OFFERED, N.COMPLETED),
        (N.COMPLETED, N.OFFERED),
        (N.ESCALATED, N.REQUESTED),
        (N.EXPIRED, N.OFFERED),
        (N.DECLINED, N.ACCEPTED),
    ],
)
def test_illegal_negotiation_transitions_raise(current: N, target: N) -> None:
    with pytest.raises(IllegalTransition):
        advance_negotiation(current, target)


def test_a_plan_cannot_be_accepted_before_it_is_offered() -> None:
    with pytest.raises(IllegalTransition):
        advance_negotiation(N.AWAITING_APPROVAL, N.ACCEPTED)


def test_every_negotiation_state_has_a_defined_row() -> None:
    assert NEGOTIATION_MACHINE.states == frozenset(NegotiationState)


def test_every_negotiation_can_reach_an_end() -> None:
    for state in NegotiationState:
        assert any(
            NEGOTIATION_MACHINE.is_terminal(s) for s in reachable(NEGOTIATION_MACHINE, state)
        )


def test_terminal_negotiation_states() -> None:
    ends = {state for state in NegotiationState if NEGOTIATION_MACHINE.is_terminal(state)}
    assert ends == {N.COMPLETED, N.ESCALATED, N.EXPIRED}


@pytest.mark.parametrize("intent", list(ClientIntent))
def test_every_client_intent_has_a_route(intent: ClientIntent) -> None:
    assert isinstance(route_for(intent), NegotiationRoute)


@pytest.mark.parametrize(
    ("intent", "route"),
    [
        (ClientIntent.CANNOT_PAY_IN_FULL, NegotiationRoute.PROPOSE_PLAN),
        (ClientIntent.REQUESTS_EXTENSION, NegotiationRoute.PROPOSE_PLAN),
        (ClientIntent.REQUESTS_DISCOUNT, NegotiationRoute.ESCALATE),
        (ClientIntent.DISPUTES_INVOICE, NegotiationRoute.ESCALATE),
        (ClientIntent.OTHER, NegotiationRoute.ESCALATE),
        (ClientIntent.ACCEPTS_OFFER, NegotiationRoute.RECORD_ACCEPTANCE),
        (ClientIntent.DECLINES_OFFER, NegotiationRoute.RECORD_DECLINE),
        (ClientIntent.WILL_PAY_SOON, NegotiationRoute.NO_ACTION),
    ],
)
def test_intents_route_to_the_expected_action(
    intent: ClientIntent, route: NegotiationRoute
) -> None:
    assert route_for(intent) == route
