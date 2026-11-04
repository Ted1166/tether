from collections.abc import Sequence
from typing import Any


class PayPalError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        name: str | None = None,
        debug_id: str | None = None,
        issues: Sequence[str] = (),
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.name = name
        self.debug_id = debug_id
        self.issues = tuple(issues)

    def __str__(self) -> str:
        head = " ".join(part for part in (str(self.status or ""), self.name or "") if part)
        text = f"{head}: {self.message}" if head else self.message
        if self.issues:
            text += f" ({'; '.join(self.issues)})"
        if self.debug_id:
            text += f" [debug_id {self.debug_id}]"
        return text


class PayPalAuthError(PayPalError):
    pass


class PayPalNotFound(PayPalError):
    pass


class PayPalRateLimited(PayPalError):
    pass


class PayPalRequestError(PayPalError):
    pass


class PayPalUnavailable(PayPalError):
    pass


class PayPalResponseError(PayPalError):
    pass


def from_response(status: int, payload: object) -> PayPalError:
    body: dict[str, Any] = payload if isinstance(payload, dict) else {}
    raw_name = body.get("name") or body.get("error")
    raw_debug = body.get("debug_id")
    message = str(
        body.get("message") or body.get("error_description") or f"PayPal returned {status}"
    )
    name = str(raw_name) if raw_name else None
    debug_id = str(raw_debug) if raw_debug else None
    issues = _issues(body.get("details"))

    kind: type[PayPalError] = PayPalRequestError
    if status in (401, 403):
        kind = PayPalAuthError
    elif status == 404:
        kind = PayPalNotFound
    elif status == 429:
        kind = PayPalRateLimited
    elif status >= 500:
        kind = PayPalUnavailable
    return kind(message, status=status, name=name, debug_id=debug_id, issues=issues)


def _issues(details: object) -> list[str]:
    if not isinstance(details, list):
        return []
    issues: list[str] = []
    for entry in details:
        if not isinstance(entry, dict):
            continue
        field = entry.get("field")
        issue = entry.get("issue") or entry.get("description")
        if field and issue:
            issues.append(f"{field}: {issue}")
        elif issue:
            issues.append(str(issue))
    return issues
