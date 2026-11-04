from decimal import Decimal

from hypothesis import given, settings

from app.policy.engine import evaluate
from app.policy.negotiation import closest_plan, generate_plans, plan_to_action
from app.schemas.actions import Verdict
from app.schemas.policy import Policy
from tests.factories import make_context
from tests.strategies import CLIENT, plan_policies, totals

PROPERTY_SETTINGS = settings(max_examples=400, deadline=None)


@given(totals, plan_policies())
@PROPERTY_SETTINGS
def test_generated_plans_always_respect_the_policy(total: Decimal, policy: Policy) -> None:
    limits = policy.plans
    floor = max(limits.min_installment_amount, Decimal("0.01"))
    for plan in generate_plans(total, policy):
        assert 2 <= plan.installment_count <= limits.max_installments
        assert plan.upfront_percent >= limits.min_upfront_percent
        assert plan.days_out <= limits.max_days_out
        assert all(item.amount >= floor for item in plan.installments)


@given(totals, plan_policies())
@PROPERTY_SETTINGS
def test_generated_plans_always_add_up_exactly(total: Decimal, policy: Policy) -> None:
    for plan in generate_plans(total, policy):
        assert sum((item.amount for item in plan.installments), Decimal(0)) == plan.total
        assert plan.total == total.quantize(Decimal("0.01"))


@given(totals, plan_policies())
@PROPERTY_SETTINGS
def test_the_engine_never_blocks_a_generated_plan(total: Decimal, policy: Policy) -> None:
    for plan in generate_plans(total, policy):
        decision = evaluate(plan_to_action(plan, CLIENT), policy, make_context())
        assert decision.verdict is not Verdict.BLOCK


@given(totals, plan_policies())
@PROPERTY_SETTINGS
def test_generation_is_deterministic(total: Decimal, policy: Policy) -> None:
    assert generate_plans(total, policy) == generate_plans(total, policy)


@given(totals, plan_policies())
@PROPERTY_SETTINGS
def test_closest_plan_only_ever_returns_a_generated_plan(total: Decimal, policy: Policy) -> None:
    plans = generate_plans(total, policy)
    chosen = closest_plan(plans, installments=7, days_out=500)
    assert (chosen is None) == (not plans)
    if chosen is not None:
        assert chosen in plans
