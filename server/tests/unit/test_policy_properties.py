from datetime import UTC, datetime, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from app.policy.engine import evaluate
from app.schemas.actions import (
    ActionContext,
    ActionKind,
    InvoiceStatus,
    ProposedAction,
    Verdict,
)
from app.schemas.policy import (
    AmountPolicy,
    ContactMode,
    ContactPolicy,
    PlanPolicy,
    Policy,
    QuietHours,
    ReminderPolicy,
)

EMAILS = ["dan@example.com", "maya@example.com", "kofi@example.com", "ruth@example.com"]

amounts = st.decimals(min_value=0, max_value=20000, places=2)
kinds = st.sampled_from(list(ActionKind))
emails = st.sampled_from(EMAILS)


@st.composite
def policies(draw: st.DrawFn) -> Policy:
    threshold = draw(st.decimals(min_value=0, max_value=2000, places=2))
    cap = threshold + draw(st.decimals(min_value=0, max_value=10000, places=2))
    allowed = frozenset(draw(st.sets(emails, max_size=3)))
    blocked = frozenset(draw(st.sets(emails, max_size=2)))
    auto = frozenset(draw(st.sets(kinds, max_size=3)))
    disabled = frozenset(draw(st.sets(kinds, max_size=2))) - auto
    return Policy(
        version=draw(st.integers(min_value=1, max_value=50)),
        contacts=ContactPolicy(
            mode=draw(st.sampled_from(list(ContactMode))), allowed=allowed, blocked=blocked
        ),
        reminders=ReminderPolicy(
            max_per_week=draw(st.integers(0, 5)),
            max_total=draw(st.integers(0, 10)),
            min_days_overdue=draw(st.integers(0, 14)),
        ),
        amounts=AmountPolicy(auto_approve_below=threshold, hard_cap=cap),
        plans=PlanPolicy(
            max_installments=draw(st.integers(1, 12)),
            min_upfront_percent=draw(st.integers(0, 100)),
            max_days_out=draw(st.integers(0, 180)),
            max_discount_percent=draw(st.integers(0, 30)),
        ),
        quiet_hours=QuietHours(
            enabled=draw(st.booleans()),
            start_hour=draw(st.integers(0, 23)),
            end_hour=draw(st.integers(0, 23)),
            timezone=draw(st.sampled_from(["UTC", "Africa/Nairobi", "America/New_York"])),
        ),
        auto_actions=auto,
        disabled_actions=disabled,
    )


@st.composite
def actions(draw: st.DrawFn) -> ProposedAction:
    return ProposedAction(
        kind=draw(kinds),
        client_email=draw(emails),
        amount=draw(amounts),
        installments=draw(st.none() | st.integers(1, 20)),
        upfront_percent=draw(st.none() | st.integers(0, 100)),
        days_out=draw(st.none() | st.integers(0, 400)),
        discount_percent=draw(st.integers(0, 100)),
    )


@st.composite
def contexts(draw: st.DrawFn) -> ActionContext:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    return ActionContext(
        now=base + timedelta(minutes=draw(st.integers(0, 60 * 24 * 365))),
        invoice_status=draw(st.none() | st.sampled_from(list(InvoiceStatus))),
        days_overdue=draw(st.integers(0, 90)),
        reminders_last_7_days=draw(st.integers(0, 8)),
        reminders_total=draw(st.integers(0, 15)),
    )


PROPERTY_SETTINGS = settings(max_examples=400, deadline=None)


@given(actions(), policies(), contexts())
@PROPERTY_SETTINGS
def test_a_plan_that_is_not_blocked_never_breaks_the_limits(
    action: ProposedAction, policy: Policy, context: ActionContext
) -> None:
    if action.kind is not ActionKind.OFFER_PAYMENT_PLAN:
        return
    decision = evaluate(action, policy, context)
    if decision.verdict is Verdict.BLOCK:
        return
    assert action.installments is not None
    assert action.upfront_percent is not None
    assert action.days_out is not None
    assert action.installments <= policy.plans.max_installments
    assert action.upfront_percent >= policy.plans.min_upfront_percent
    assert action.days_out <= policy.plans.max_days_out
    assert action.discount_percent <= policy.plans.max_discount_percent


@given(actions(), policies(), contexts())
@PROPERTY_SETTINGS
def test_nothing_unapproved_ever_reaches_a_stranger(
    action: ProposedAction, policy: Policy, context: ActionContext
) -> None:
    decision = evaluate(action, policy, context)
    if decision.verdict is Verdict.BLOCK:
        return
    assert action.client_email not in policy.contacts.blocked
    if policy.contacts.mode is ContactMode.ALLOWLIST:
        assert action.client_email in policy.contacts.allowed


@given(actions(), policies(), contexts())
@PROPERTY_SETTINGS
def test_amounts_above_the_hard_cap_are_always_blocked(
    action: ProposedAction, policy: Policy, context: ActionContext
) -> None:
    decision = evaluate(action, policy, context)
    if action.amount > policy.amounts.hard_cap:
        assert decision.verdict is Verdict.BLOCK


@given(actions(), policies(), contexts())
@PROPERTY_SETTINGS
def test_disabled_actions_are_always_blocked(
    action: ProposedAction, policy: Policy, context: ActionContext
) -> None:
    decision = evaluate(action, policy, context)
    if action.kind in policy.disabled_actions:
        assert decision.verdict is Verdict.BLOCK


@given(actions(), policies(), contexts())
@PROPERTY_SETTINGS
def test_allow_means_the_policy_explicitly_trusted_the_action(
    action: ProposedAction, policy: Policy, context: ActionContext
) -> None:
    decision = evaluate(action, policy, context)
    if decision.verdict is Verdict.ALLOW:
        assert action.kind in policy.auto_actions
        assert action.amount < policy.amounts.auto_approve_below
        assert decision.triggered_rules == ()


@given(actions(), policies(), contexts())
@PROPERTY_SETTINGS
def test_reminders_never_exceed_the_cadence_limits(
    action: ProposedAction, policy: Policy, context: ActionContext
) -> None:
    if action.kind is not ActionKind.SEND_REMINDER:
        return
    decision = evaluate(action, policy, context)
    if decision.verdict is Verdict.BLOCK:
        return
    assert context.reminders_last_7_days < policy.reminders.max_per_week
    assert context.reminders_total < policy.reminders.max_total
    assert context.days_overdue >= policy.reminders.min_days_overdue


@given(actions(), policies(), contexts())
@PROPERTY_SETTINGS
def test_evaluation_is_deterministic(
    action: ProposedAction, policy: Policy, context: ActionContext
) -> None:
    assert evaluate(action, policy, context) == evaluate(action, policy, context)


@given(actions(), policies(), contexts())
@PROPERTY_SETTINGS
def test_every_decision_explains_itself(
    action: ProposedAction, policy: Policy, context: ActionContext
) -> None:
    decision = evaluate(action, policy, context)
    assert decision.reason
    assert decision.rule
    assert decision.policy_version == policy.version
    if decision.verdict is not Verdict.ALLOW:
        assert decision.rule in decision.triggered_rules
