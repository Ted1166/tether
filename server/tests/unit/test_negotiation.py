from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.policy.engine import evaluate
from app.policy.negotiation import closest_plan, generate_plans, plan_to_action
from app.schemas.actions import ActionKind, Verdict
from app.schemas.negotiation import Installment, PaymentPlanOffer
from app.schemas.policy import PlanPolicy, Policy
from tests.factories import make_context, make_plan

TOTAL = Decimal("900.00")


def with_plans(policy: Policy, **changes: object) -> Policy:
    return policy.model_copy(update={"plans": policy.plans.model_copy(update=changes)})


def test_default_policy_produces_plans_within_every_limit(policy: Policy) -> None:
    plans = generate_plans(TOTAL, policy)
    assert plans
    for plan in plans:
        assert plan.installment_count <= policy.plans.max_installments
        assert plan.upfront_percent >= policy.plans.min_upfront_percent
        assert plan.days_out <= policy.plans.max_days_out
        assert sum(item.amount for item in plan.installments) == TOTAL


def test_simplest_plan_comes_first(policy: Policy) -> None:
    first = generate_plans(TOTAL, policy)[0]
    assert first.installment_count == 2
    assert first.upfront_percent == 50


def test_three_part_plan_is_available_under_defaults(policy: Policy) -> None:
    counts = {plan.installment_count for plan in generate_plans(TOTAL, policy)}
    assert counts == {2, 3}


def test_plan_schedule_starts_today_and_ends_at_the_limit(policy: Policy) -> None:
    plan = next(p for p in generate_plans(TOTAL, policy) if p.installment_count == 3)
    assert [item.due_in_days for item in plan.installments] == [0, 30, 60]


def test_odd_cents_are_shared_out_and_nothing_is_lost(policy: Policy) -> None:
    total = Decimal("100.01")
    for plan in generate_plans(total, policy):
        assert sum(item.amount for item in plan.installments) == total


def test_upfront_is_rounded_up_so_the_minimum_always_holds(policy: Policy) -> None:
    for plan in generate_plans(Decimal("333.33"), policy):
        assert plan.upfront_percent >= policy.plans.min_upfront_percent


def test_amounts_too_small_to_split_produce_no_plans(policy: Policy) -> None:
    assert generate_plans(Decimal("30.00"), policy) == ()


def test_zero_total_produces_no_plans(policy: Policy) -> None:
    assert generate_plans(Decimal("0"), policy) == ()


def test_single_installment_policy_produces_no_plans(policy: Policy) -> None:
    assert generate_plans(TOTAL, with_plans(policy, max_installments=1)) == ()


def test_a_horizon_shorter_than_the_installments_allow_is_skipped(policy: Policy) -> None:
    plans = generate_plans(TOTAL, with_plans(policy, max_days_out=1))
    assert {plan.installment_count for plan in plans} == {2}


def test_closest_plan_matches_the_requested_count(policy: Policy) -> None:
    plans = generate_plans(TOTAL, policy)
    chosen = closest_plan(plans, installments=3)
    assert chosen is not None
    assert chosen.installment_count == 3


def test_closest_plan_never_exceeds_the_limit_when_the_client_asks_for_more(
    policy: Policy,
) -> None:
    chosen = closest_plan(generate_plans(TOTAL, policy), installments=6)
    assert chosen is not None
    assert chosen.installment_count == policy.plans.max_installments


def test_closest_plan_follows_the_requested_timeline(policy: Policy) -> None:
    short = with_plans(policy, max_days_out=30)
    chosen = closest_plan(generate_plans(TOTAL, short), days_out=90)
    assert chosen is not None
    assert chosen.days_out == 30


def test_closest_plan_without_a_request_returns_the_default(policy: Policy) -> None:
    plans = generate_plans(TOTAL, policy)
    assert closest_plan(plans) == plans[0]


def test_closest_plan_with_no_plans_returns_nothing() -> None:
    assert closest_plan(()) is None


def test_every_generated_plan_is_acceptable_to_the_engine(policy: Policy) -> None:
    for plan in generate_plans(TOTAL, policy):
        action = plan_to_action(plan, "dan@example.com")
        decision = evaluate(action, policy, make_context())
        assert decision.verdict is Verdict.NEEDS_APPROVAL
        assert decision.rule == "approval_required"


def test_plan_to_action_carries_the_plan_terms(policy: Policy) -> None:
    plan = generate_plans(TOTAL, policy)[0]
    action = plan_to_action(plan, "Dan@Example.com")
    assert action.kind is ActionKind.OFFER_PAYMENT_PLAN
    assert action.client_email == "dan@example.com"
    assert action.amount == TOTAL
    assert action.installments == plan.installment_count
    assert action.upfront_percent == plan.upfront_percent
    assert action.days_out == plan.days_out
    assert action.discount_percent == 0


def test_engine_blocks_installments_below_the_minimum_amount(policy: Policy) -> None:
    decision = evaluate(make_plan(amount="60.00", installments=3), policy, make_context())
    assert decision.verdict is Verdict.BLOCK
    assert decision.rule == "plan_installments_too_small"


def test_engine_allows_installments_exactly_at_the_minimum(policy: Policy) -> None:
    decision = evaluate(make_plan(amount="75.00", installments=3), policy, make_context())
    assert decision.verdict is Verdict.NEEDS_APPROVAL


def test_minimum_installment_default_is_twenty_five() -> None:
    assert PlanPolicy().min_installment_amount == Decimal("25")


def installment(number: int, amount: str, days: int) -> Installment:
    return Installment(number=number, amount=Decimal(amount), due_in_days=days)


def test_offer_rejects_installments_that_do_not_add_up() -> None:
    with pytest.raises(ValidationError):
        PaymentPlanOffer(
            currency="USD",
            total=Decimal("100"),
            installments=(installment(1, "40", 0), installment(2, "50", 30)),
        )


def test_offer_rejects_due_dates_that_do_not_increase() -> None:
    with pytest.raises(ValidationError):
        PaymentPlanOffer(
            currency="USD",
            total=Decimal("100"),
            installments=(installment(1, "50", 10), installment(2, "50", 10)),
        )


def test_offer_rejects_misnumbered_installments() -> None:
    with pytest.raises(ValidationError):
        PaymentPlanOffer(
            currency="USD",
            total=Decimal("100"),
            installments=(installment(2, "50", 0), installment(3, "50", 30)),
        )


def test_offer_requires_at_least_two_installments() -> None:
    with pytest.raises(ValidationError):
        PaymentPlanOffer(
            currency="USD", total=Decimal("100"), installments=(installment(1, "100", 0),)
        )
