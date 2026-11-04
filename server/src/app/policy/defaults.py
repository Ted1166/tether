from collections.abc import Iterable

from app.schemas.actions import ActionKind
from app.schemas.policy import ContactPolicy, Policy, QuietHours


def starter_policy(allowed_emails: Iterable[str] = (), timezone: str = "UTC") -> Policy:
    return Policy(
        contacts=ContactPolicy(allowed=frozenset(allowed_emails)),
        quiet_hours=QuietHours(timezone=timezone),
        auto_actions=frozenset({ActionKind.CREATE_INVOICE}),
    )
