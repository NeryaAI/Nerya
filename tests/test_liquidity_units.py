from decimal import Decimal

import pytest

from nerya.financial.defi.liquidity import token_amounts,pool_price,position_ticks
from nerya.financial.contracts import FinancialError

pytestmark=pytest.mark.smoke


def test_liquidity_inside_and_outside_range_preserves_two_token_units():
    a,b=token_amounts(1000000,2**96,-100,100)
    assert a>0 and b>0 and abs(a-b)<Decimal("1e-80")
    below=token_amounts(1000000,2**95,-100,100)
    above=token_amounts(1000000,2**97,-100,100)
    assert below[0]>0 and below[1]==0
    assert above[0]==0 and above[1]>0


def test_pool_price_applies_asset_decimal_difference():
    assert pool_price(2**96,18,6)==10**12
    assert pool_price(2**96,6,18)==Decimal("1e-12")


def test_v4_packed_ticks_are_signed_at_official_offsets():
    value=(((-100)&((1<<24)-1))<<8)|(100<<32)|(1<<255)
    assert position_ticks(value)==(-100,100)


def test_liquidity_rejects_invalid_values_instead_of_returning_empty_position():
    with pytest.raises(FinancialError):token_amounts(1,0,-100,100)
    with pytest.raises(FinancialError):token_amounts(1,2**96,100,-100)
