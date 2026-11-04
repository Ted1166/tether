from decimal import ROUND_HALF_UP, Decimal

from app.schemas.actions import ActionKind, ProposedAction
from app.schemas.negotiation import Installment, PaymentPlanOffer
from app.schemas.policy import PlanPolicy, Policy

CENT = Decimal("0.01")
BALANCED_UPFRONT_PERCENT = 50


def generate_plans(total: Decimal, policy: Policy) -> tuple[PaymentPlanOffer, ...]:
    limits = policy.plans
    total_cents = _to_cents(total)
    if total_cents <= 0:
        return ()

    offers: list[PaymentPlanOffer] = []
    for count in range(2, limits.max_installments + 1):
        for percent in _upfront_tiers(limits):
            offer = _build(total_cents, count, percent, policy)
            if offer is not None:
                offers.append(offer)

    offers.sort(key=lambda offer: (offer.installment_count, -offer.upfront_percent))
    return tuple(offers)


def closest_plan(
    plans: tuple[PaymentPlanOffer, ...],
    installments: int | None = None,
    days_out: int | None = None,
) -> PaymentPlanOffer | None:
    if not plans:
        return None

    def distance(entry: tuple[int, PaymentPlanOffer]) -> tuple[int, int, int]:
        position, offer = entry
        count_gap = abs(offer.installment_count - installments) if installments else 0
        days_gap = abs(offer.days_out - days_out) if days_out is not None else 0
        return (count_gap, days_gap, position)

    return min(enumerate(plans), key=distance)[1]


def plan_to_action(
    offer: PaymentPlanOffer,
    client_email: str,
    kind: ActionKind = ActionKind.OFFER_PAYMENT_PLAN,
) -> ProposedAction:
    return ProposedAction(
        kind=kind,
        client_email=client_email,
        amount=offer.total,
        installments=offer.installment_count,
        upfront_percent=offer.upfront_percent,
        days_out=offer.days_out,
        discount_percent=0,
    )


def _to_cents(value: Decimal) -> int:
    return int(value.quantize(CENT, rounding=ROUND_HALF_UP) * 100)


def _from_cents(cents: int) -> Decimal:
    return Decimal(cents).scaleb(-2).quantize(CENT)


def _upfront_tiers(limits: PlanPolicy) -> list[int]:
    return sorted(
        {limits.min_upfront_percent, max(limits.min_upfront_percent, BALANCED_UPFRONT_PERCENT)}
    )


def _build(
    total_cents: int, count: int, upfront_percent: int, policy: Policy
) -> PaymentPlanOffer | None:
    limits = policy.plans
    later_payments = count - 1
    if limits.max_days_out < later_payments:
        return None

    floor = max(_to_cents(limits.min_installment_amount), 1)
    upfront = -(-total_cents * upfront_percent // 100)
    remainder = total_cents - upfront
    if upfront < floor or remainder < later_payments * floor:
        return None

    base, extra = divmod(remainder, later_payments)
    amounts = [upfront] + [base + (1 if index < extra else 0) for index in range(later_payments)]
    days = [index * limits.max_days_out // later_payments for index in range(count)]

    return PaymentPlanOffer(
        currency=policy.currency,
        total=_from_cents(total_cents),
        installments=tuple(
            Installment(number=position + 1, amount=_from_cents(cents), due_in_days=due)
            for position, (cents, due) in enumerate(zip(amounts, days, strict=True))
        ),
    )
