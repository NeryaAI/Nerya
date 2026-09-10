"""F11 invariant: on-chain base-unit conversions must be float-exact."""

import pytest

from nerya.wallet.amounts import to_base_units, to_base_units_ceil


@pytest.mark.smoke
def test_to_base_units_exact_where_float_truncates():
    # int(float(8.1) * 10**18) == 8099999999999999488 — the F11 bug class.
    assert to_base_units(8.1, 18) == 8_100_000_000_000_000_000
    assert to_base_units(6.9, 18) == 6_900_000_000_000_000_000
    assert to_base_units("1.5", 6) == 1_500_000
    assert to_base_units(2, 0) == 2


@pytest.mark.smoke
def test_approved_floor_rounds_up_never_down():
    # A floor below the approved amount would let a tx fill under approval.
    assert to_base_units_ceil("1.2345678", 6) == 1_234_568
    assert to_base_units("1.2345678", 6) == 1_234_567
    assert to_base_units_ceil(2, 0) == 2
