from decimal import Decimal

from hypothesis import strategies as st

from app.schemas.policy import AmountPolicy, ContactPolicy, PlanPolicy, Policy

CLIENT = "dan@example.com"


@st.composite
def plan_policies(draw: st.DrawFn) -> Policy:
    return Policy(
        contacts=ContactPolicy(allowed=frozenset({CLIENT})),
        amounts=AmountPolicy(auto_approve_below=Decimal("200"), hard_cap=Decimal("1000000")),
        plans=PlanPolicy(
            max_installments=draw(st.integers(1, 12)),
            min_upfront_percent=draw(st.integers(0, 100)),
            max_days_out=draw(st.integers(0, 365)),
            max_discount_percent=0,
            min_installment_amount=draw(st.decimals(min_value=0, max_value=200, places=2)),
        ),
    )


totals = st.decimals(min_value=0, max_value=50000, places=2)
