from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.lib.money import from_cents, to_cents


@pytest.mark.parametrize(
    ("value", "cents"),
    [
        ("0", 0),
        ("0.01", 1),
        ("40", 4000),
        ("133.50", 13350),
        ("10.005", 1001),
        ("10.004", 1000),
        ("1234567.89", 123456789),
    ],
)
def test_amounts_convert_to_whole_cents(value: str, cents: int) -> None:
    assert to_cents(Decimal(value)) == cents


def test_cents_convert_back_to_exact_two_place_decimals() -> None:
    assert from_cents(13350) == Decimal("133.50")
    assert str(from_cents(5)) == "0.05"
    assert str(from_cents(4000)) == "40.00"


@given(st.integers(min_value=0, max_value=10**12))
def test_round_trip_never_loses_a_cent(cents: int) -> None:
    assert to_cents(from_cents(cents)) == cents


def test_binary_float_style_sums_stay_exact() -> None:
    total = from_cents(to_cents(Decimal("0.10")) + to_cents(Decimal("0.20")))
    assert total == Decimal("0.30")
