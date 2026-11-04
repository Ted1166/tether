import pytest

from app.policy.engine import evaluate
from app.schemas.actions import ActionKind, InvoiceStatus, Verdict
from app.schemas.policy import ContactMode, ContactPolicy, Policy, QuietHours
from tests.factories import (
    EARLY_MORNING,
    NIGHT,
    make_action,
    make_context,
    make_plan,
)


def reminder_policy(policy: Policy) -> Policy:
    return policy.model_copy(
        update={"auto_actions": frozenset({ActionKind.SEND_REMINDER, ActionKind.CREATE_INVOICE})}
    )


def test_small_invoice_to_known_client_is_allowed(policy: Policy) -> None:
    decision = evaluate(make_action(), policy, make_context())
    assert decision.verdict is Verdict.ALLOW
    assert decision.rule == "within_policy"


def test_decision_records_the_policy_version(policy: Policy) -> None:
    versioned = policy.model_copy(update={"version": 7})
    decision = evaluate(make_action(), versioned, make_context())
    assert decision.policy_version == 7


def test_unknown_client_is_blocked_in_allowlist_mode(policy: Policy) -> None:
    decision = evaluate(make_action(client_email="stranger@example.com"), policy, make_context())
    assert decision.verdict is Verdict.BLOCK
    assert decision.rule == "contact_not_allowed"


def test_unknown_client_is_fine_when_anyone_is_allowed() -> None:
    open_policy = Policy(contacts=ContactPolicy(mode=ContactMode.ANYONE))
    decision = evaluate(
        make_action(client_email="stranger@example.com"), open_policy, make_context()
    )
    assert decision.verdict is Verdict.ALLOW


def test_blocked_contact_loses_even_when_anyone_is_allowed() -> None:
    open_policy = Policy(
        contacts=ContactPolicy(mode=ContactMode.ANYONE, blocked=frozenset({"dan@example.com"}))
    )
    decision = evaluate(make_action(), open_policy, make_context())
    assert decision.verdict is Verdict.BLOCK
    assert decision.rule == "contact_blocked"


def test_amount_above_hard_cap_is_blocked(policy: Policy) -> None:
    decision = evaluate(make_action(amount="5000.01"), policy, make_context())
    assert decision.verdict is Verdict.BLOCK
    assert decision.rule == "amount_over_cap"


def test_amount_exactly_at_hard_cap_needs_approval_not_block(policy: Policy) -> None:
    decision = evaluate(make_action(amount="5000"), policy, make_context())
    assert decision.verdict is Verdict.NEEDS_APPROVAL
    assert decision.rule == "amount_needs_approval"


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        ("199.99", Verdict.ALLOW),
        ("200", Verdict.NEEDS_APPROVAL),
        ("1200", Verdict.NEEDS_APPROVAL),
    ],
)
def test_auto_approve_threshold(policy: Policy, amount: str, expected: Verdict) -> None:
    decision = evaluate(make_action(amount=amount), policy, make_context())
    assert decision.verdict is expected


def test_disabled_action_is_blocked(policy: Policy) -> None:
    decision = evaluate(make_action(kind=ActionKind.CANCEL_INVOICE), policy, make_context())
    assert decision.verdict is Verdict.BLOCK
    assert decision.rule == "action_disabled"


def test_actions_outside_the_auto_list_need_approval(policy: Policy) -> None:
    decision = evaluate(
        make_action(kind=ActionKind.SEND_INVOICE),
        policy,
        make_context(invoice_status=InvoiceStatus.DRAFT),
    )
    assert decision.verdict is Verdict.NEEDS_APPROVAL
    assert decision.rule == "approval_required"


def test_reminder_needs_approval_by_default(policy: Policy) -> None:
    decision = evaluate(make_action(kind=ActionKind.SEND_REMINDER), policy, make_context())
    assert decision.verdict is Verdict.NEEDS_APPROVAL


def test_reminder_is_allowed_when_the_policy_trusts_it(policy: Policy) -> None:
    decision = evaluate(
        make_action(kind=ActionKind.SEND_REMINDER), reminder_policy(policy), make_context()
    )
    assert decision.verdict is Verdict.ALLOW


@pytest.mark.parametrize(
    ("overrides", "rule"),
    [
        ({"days_overdue": 0}, "reminder_too_early"),
        ({"reminders_last_7_days": 2}, "reminder_weekly_limit"),
        ({"reminders_total": 6}, "reminder_total_limit"),
    ],
)
def test_reminder_cadence_limits(policy: Policy, overrides: dict[str, int], rule: str) -> None:
    decision = evaluate(
        make_action(kind=ActionKind.SEND_REMINDER),
        reminder_policy(policy),
        make_context(**overrides),
    )
    assert decision.verdict is Verdict.BLOCK
    assert decision.rule == rule


@pytest.mark.parametrize(
    "status",
    [InvoiceStatus.PAID, InvoiceStatus.CANCELLED, InvoiceStatus.DRAFT, None],
)
def test_reminders_are_blocked_unless_money_is_actually_owed(
    policy: Policy, status: InvoiceStatus | None
) -> None:
    decision = evaluate(
        make_action(kind=ActionKind.SEND_REMINDER),
        reminder_policy(policy),
        make_context(invoice_status=status),
    )
    assert decision.verdict is Verdict.BLOCK
    assert decision.rule == "invoice_not_collectable"


def test_valid_payment_plan_waits_for_approval(policy: Policy) -> None:
    decision = evaluate(make_plan(), policy, make_context())
    assert decision.verdict is Verdict.NEEDS_APPROVAL
    assert decision.rule == "approval_required"


@pytest.mark.parametrize(
    ("overrides", "rule"),
    [
        ({"installments": 4}, "plan_too_many_installments"),
        ({"upfront_percent": 24}, "plan_upfront_too_low"),
        ({"days_out": 61}, "plan_too_long"),
        ({"installments": None}, "plan_incomplete"),
        ({"upfront_percent": None}, "plan_incomplete"),
        ({"days_out": None}, "plan_incomplete"),
        ({"discount_percent": 50}, "discount_over_limit"),
    ],
)
def test_payment_plans_outside_the_limits_are_blocked(
    policy: Policy, overrides: dict[str, int | None], rule: str
) -> None:
    decision = evaluate(make_plan(**overrides), policy, make_context())
    assert decision.verdict is Verdict.BLOCK
    assert decision.rule == rule


def test_plan_exactly_on_the_limits_is_acceptable(policy: Policy) -> None:
    decision = evaluate(
        make_plan(installments=3, upfront_percent=25, days_out=60), policy, make_context()
    )
    assert decision.verdict is Verdict.NEEDS_APPROVAL


def test_discount_is_allowed_when_the_policy_permits_it(policy: Policy) -> None:
    generous = policy.model_copy(
        update={"plans": policy.plans.model_copy(update={"max_discount_percent": 10})}
    )
    decision = evaluate(
        make_action(kind=ActionKind.APPLY_DISCOUNT, discount_percent=10), generous, make_context()
    )
    assert decision.verdict is Verdict.NEEDS_APPROVAL
    assert decision.rule == "approval_required"


def test_plan_on_a_paid_invoice_is_blocked(policy: Policy) -> None:
    decision = evaluate(make_plan(), policy, make_context(invoice_status=InvoiceStatus.PAID))
    assert decision.verdict is Verdict.BLOCK
    assert decision.rule == "invoice_closed"


def test_quiet_hours_pull_automatic_sends_back_to_approval(policy: Policy) -> None:
    trusting = policy.model_copy(update={"auto_actions": frozenset({ActionKind.SEND_INVOICE})})
    action = make_action(kind=ActionKind.SEND_INVOICE)
    context = make_context(now=NIGHT, invoice_status=InvoiceStatus.DRAFT)
    decision = evaluate(action, trusting, context)
    assert decision.verdict is Verdict.NEEDS_APPROVAL
    assert decision.rule == "quiet_hours"


def test_quiet_hours_wrap_past_midnight(policy: Policy) -> None:
    trusting = policy.model_copy(update={"auto_actions": frozenset({ActionKind.SEND_INVOICE})})
    action = make_action(kind=ActionKind.SEND_INVOICE)
    context = make_context(now=EARLY_MORNING, invoice_status=InvoiceStatus.DRAFT)
    assert evaluate(action, trusting, context).rule == "quiet_hours"


def test_quiet_hours_respect_the_policy_timezone(policy: Policy) -> None:
    nairobi = policy.model_copy(
        update={
            "auto_actions": frozenset({ActionKind.SEND_INVOICE}),
            "quiet_hours": QuietHours(timezone="Africa/Nairobi"),
        }
    )
    action = make_action(kind=ActionKind.SEND_INVOICE)
    context = make_context(now=NIGHT.replace(hour=19), invoice_status=InvoiceStatus.DRAFT)
    assert evaluate(action, nairobi, context).rule == "quiet_hours"


def test_quiet_hours_can_be_switched_off(policy: Policy) -> None:
    trusting = policy.model_copy(
        update={
            "auto_actions": frozenset({ActionKind.SEND_INVOICE}),
            "quiet_hours": QuietHours(enabled=False),
        }
    )
    action = make_action(kind=ActionKind.SEND_INVOICE)
    context = make_context(now=NIGHT, invoice_status=InvoiceStatus.DRAFT)
    assert evaluate(action, trusting, context).verdict is Verdict.ALLOW


def test_quiet_hours_ignore_actions_that_send_nothing(policy: Policy) -> None:
    decision = evaluate(make_action(), policy, make_context(now=NIGHT))
    assert decision.verdict is Verdict.ALLOW


def test_a_block_outranks_an_approval_request(policy: Policy) -> None:
    decision = evaluate(make_action(amount="9000"), policy, make_context())
    assert decision.verdict is Verdict.BLOCK
    assert decision.rule == "amount_over_cap"
    assert "amount_needs_approval" in decision.triggered_rules


def test_every_triggered_rule_is_reported(policy: Policy) -> None:
    decision = evaluate(
        make_plan(client_email="stranger@example.com", installments=9), policy, make_context()
    )
    assert decision.verdict is Verdict.BLOCK
    assert {"contact_not_allowed", "plan_too_many_installments"} <= set(decision.triggered_rules)
