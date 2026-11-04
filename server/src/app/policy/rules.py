from collections.abc import Callable
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from app.schemas.actions import (
    ActionContext,
    ActionKind,
    InvoiceStatus,
    ProposedAction,
    Verdict,
)
from app.schemas.policy import ContactMode, Policy


@dataclass(frozen=True)
class Finding:
    verdict: Verdict
    rule: str
    reason: str


Rule = Callable[[ProposedAction, Policy, ActionContext], Finding | None]

OUTBOUND_KINDS = frozenset(
    {ActionKind.SEND_INVOICE, ActionKind.SEND_REMINDER, ActionKind.OFFER_PAYMENT_PLAN}
)
REMINDABLE_STATUSES = frozenset(
    {InvoiceStatus.SENT, InvoiceStatus.PARTIALLY_PAID, InvoiceStatus.OVERDUE}
)
CLOSED_STATUSES = frozenset({InvoiceStatus.PAID, InvoiceStatus.CANCELLED})


def _block(rule: str, reason: str) -> Finding:
    return Finding(Verdict.BLOCK, rule, reason)


def _review(rule: str, reason: str) -> Finding:
    return Finding(Verdict.NEEDS_APPROVAL, rule, reason)


def _money(policy: Policy, amount: object) -> str:
    return f"{policy.currency} {amount:,.2f}"


def action_enabled(
    action: ProposedAction, policy: Policy, context: ActionContext
) -> Finding | None:
    if action.kind in policy.disabled_actions:
        return _block("action_disabled", f"{action.kind.value} is turned off in your policy")
    return None


def contact_allowed(
    action: ProposedAction, policy: Policy, context: ActionContext
) -> Finding | None:
    contacts = policy.contacts
    if action.client_email in contacts.blocked:
        return _block("contact_blocked", f"{action.client_email} is on your blocked list")
    if contacts.mode is ContactMode.ALLOWLIST and action.client_email not in contacts.allowed:
        return _block(
            "contact_not_allowed", f"{action.client_email} is not on your approved contact list"
        )
    return None


def amount_within_cap(
    action: ProposedAction, policy: Policy, context: ActionContext
) -> Finding | None:
    cap = policy.amounts.hard_cap
    if action.amount > cap:
        return _block(
            "amount_over_cap",
            f"{_money(policy, action.amount)} is above your hard cap of {_money(policy, cap)}",
        )
    return None


def invoice_state(action: ProposedAction, policy: Policy, context: ActionContext) -> Finding | None:
    status = context.invoice_status
    if action.kind is ActionKind.SEND_REMINDER and status not in REMINDABLE_STATUSES:
        label = status.value if status else "unknown"
        return _block("invoice_not_collectable", f"an invoice that is {label} cannot be chased")
    negotiating = action.kind in {ActionKind.OFFER_PAYMENT_PLAN, ActionKind.APPLY_DISCOUNT}
    if negotiating and status in CLOSED_STATUSES:
        label = status.value if status else "unknown"
        return _block("invoice_closed", f"the invoice is already {label}")
    return None


def reminder_cadence(
    action: ProposedAction, policy: Policy, context: ActionContext
) -> Finding | None:
    if action.kind is not ActionKind.SEND_REMINDER:
        return None
    limits = policy.reminders
    if context.days_overdue < limits.min_days_overdue:
        return _block(
            "reminder_too_early",
            f"the invoice must be at least {limits.min_days_overdue} day(s) overdue",
        )
    if context.reminders_last_7_days >= limits.max_per_week:
        return _block(
            "reminder_weekly_limit",
            f"{context.reminders_last_7_days} reminders already sent this week "
            f"(limit {limits.max_per_week})",
        )
    if context.reminders_total >= limits.max_total:
        return _block(
            "reminder_total_limit",
            f"{context.reminders_total} reminders already sent (limit {limits.max_total})",
        )
    return None


def plan_terms(action: ProposedAction, policy: Policy, context: ActionContext) -> Finding | None:
    if action.kind is not ActionKind.OFFER_PAYMENT_PLAN:
        return None
    limits = policy.plans
    if action.installments is None or action.upfront_percent is None or action.days_out is None:
        return _block("plan_incomplete", "a payment plan needs installments, upfront, and a length")
    if action.installments > limits.max_installments:
        return _block(
            "plan_too_many_installments",
            f"{action.installments} installments exceeds your limit of {limits.max_installments}",
        )
    if action.upfront_percent < limits.min_upfront_percent:
        return _block(
            "plan_upfront_too_low",
            f"{action.upfront_percent}% upfront is below your minimum of "
            f"{limits.min_upfront_percent}%",
        )
    if action.days_out > limits.max_days_out:
        return _block(
            "plan_too_long",
            f"{action.days_out} days is longer than your limit of {limits.max_days_out}",
        )
    if action.amount / action.installments < limits.min_installment_amount:
        return _block(
            "plan_installments_too_small",
            f"installments would average "
            f"{_money(policy, action.amount / action.installments)}, "
            f"below your minimum of {_money(policy, limits.min_installment_amount)}",
        )
    return None


def discount_limit(
    action: ProposedAction, policy: Policy, context: ActionContext
) -> Finding | None:
    limit = policy.plans.max_discount_percent
    if action.discount_percent > limit:
        return _block(
            "discount_over_limit",
            f"a {action.discount_percent}% discount exceeds your limit of {limit}%",
        )
    return None


def _inside_window(hour: int, start: int, end: int) -> bool:
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def quiet_hours(action: ProposedAction, policy: Policy, context: ActionContext) -> Finding | None:
    window = policy.quiet_hours
    if not window.enabled or action.kind not in OUTBOUND_KINDS:
        return None
    local_hour = context.now.astimezone(ZoneInfo(window.timezone)).hour
    if _inside_window(local_hour, window.start_hour, window.end_hour):
        return _review(
            "quiet_hours",
            f"it is quiet hours ({window.start_hour:02d}:00 to {window.end_hour:02d}:00 "
            f"{window.timezone})",
        )
    return None


def autonomy(action: ProposedAction, policy: Policy, context: ActionContext) -> Finding | None:
    if action.kind not in policy.auto_actions:
        return _review("approval_required", f"{action.kind.value} always needs your approval")
    threshold = policy.amounts.auto_approve_below
    if action.amount >= threshold:
        return _review(
            "amount_needs_approval",
            f"{_money(policy, action.amount)} is at or above your auto-approve "
            f"threshold of {_money(policy, threshold)}",
        )
    return None


ALL_RULES: tuple[Rule, ...] = (
    action_enabled,
    contact_allowed,
    amount_within_cap,
    invoice_state,
    reminder_cadence,
    plan_terms,
    discount_limit,
    quiet_hours,
    autonomy,
)
