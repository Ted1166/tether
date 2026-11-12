from app.domain.machine import StateMachine
from app.schemas.records import PendingActionStatus

S = PendingActionStatus

ACTION_MACHINE = StateMachine[PendingActionStatus](
    "action",
    {
        S.PENDING: frozenset({S.EXECUTED, S.FAILED, S.REJECTED, S.BLOCKED, S.EXPIRED}),
        S.FAILED: frozenset({S.EXECUTED, S.FAILED, S.REJECTED, S.BLOCKED}),
        S.EXECUTED: frozenset(),
        S.REJECTED: frozenset(),
        S.BLOCKED: frozenset(),
        S.EXPIRED: frozenset(),
    },
)

DECIDABLE = frozenset({S.PENDING, S.FAILED})


def advance_action(
    current: PendingActionStatus, target: PendingActionStatus
) -> PendingActionStatus:
    return ACTION_MACHINE.advance(current, target)
