from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def to_cents(value: Decimal) -> int:
    return int(value.quantize(CENT, rounding=ROUND_HALF_UP) * 100)


def from_cents(cents: int) -> Decimal:
    return (Decimal(cents) / 100).quantize(CENT)
