from enum import StrEnum

from app.domain.machine import StateMachine
from app.schemas.negotiation import ClientIntent


class NegotiationState(StrEnum):
    REQUESTED = "requested"
    AWAITING_APPROVAL = "awaiting_approval"
    OFFERED = "offered"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    COMPLETED = "completed"
    ESCALATED = "escalated"
    EXPIRED = "expired"


class NegotiationRoute(StrEnum):
    PROPOSE_PLAN = "propose_plan"
    RECORD_ACCEPTANCE = "record_acceptance"
    RECORD_DECLINE = "record_decline"
    ESCALATE = "escalate"
    NO_ACTION = "no_action"


N = NegotiationState

NEGOTIATION_MACHINE = StateMachine[NegotiationState](
    "negotiation",
    {
        N.REQUESTED: frozenset({N.AWAITING_APPROVAL, N.ESCALATED}),
        N.AWAITING_APPROVAL: frozenset({N.OFFERED, N.ESCALATED, N.EXPIRED}),
        N.OFFERED: frozenset({N.ACCEPTED, N.DECLINED, N.ESCALATED, N.EXPIRED}),
        N.ACCEPTED: frozenset({N.COMPLETED, N.ESCALATED}),
        N.DECLINED: frozenset({N.ESCALATED}),
        N.COMPLETED: frozenset(),
        N.ESCALATED: frozenset(),
        N.EXPIRED: frozenset(),
    },
)

ROUTES: dict[ClientIntent, NegotiationRoute] = {
    ClientIntent.CANNOT_PAY_IN_FULL: NegotiationRoute.PROPOSE_PLAN,
    ClientIntent.REQUESTS_EXTENSION: NegotiationRoute.PROPOSE_PLAN,
    ClientIntent.REQUESTS_DISCOUNT: NegotiationRoute.ESCALATE,
    ClientIntent.DISPUTES_INVOICE: NegotiationRoute.ESCALATE,
    ClientIntent.ACCEPTS_OFFER: NegotiationRoute.RECORD_ACCEPTANCE,
    ClientIntent.DECLINES_OFFER: NegotiationRoute.RECORD_DECLINE,
    ClientIntent.WILL_PAY_SOON: NegotiationRoute.NO_ACTION,
    ClientIntent.OTHER: NegotiationRoute.ESCALATE,
}


def advance_negotiation(current: NegotiationState, target: NegotiationState) -> NegotiationState:
    return NEGOTIATION_MACHINE.advance(current, target)


def route_for(intent: ClientIntent) -> NegotiationRoute:
    return ROUTES[intent]
