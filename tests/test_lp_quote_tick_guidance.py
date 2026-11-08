"""Loaded guidance and exact-unit vectors; not a production conversion helper."""
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext

import pytest

from test_dex_exact_scope_routing import read_skill

pytestmark = pytest.mark.smoke


def test_loaded_quote_tick_guidance_preserves_units_and_identity():
    text = ' '.join(read_skill(skill='strategy_author', file='references/lp-sampled-research.md').split())
    for phrase in ('identity only', 'raw_token1/raw_token0', 'decimals1-decimals0',
                   'ordered_lower=1/quote_upper', 'floor the lower tick',
                   'ceil the upper tick', 'a CEX quote cannot establish'):
        assert phrase in text


@pytest.mark.parametrize('inverse,decimals,expected', [
    (True, (6, 18), (194700, 198120)),
    (False, (18, 6), (-198120, -194700)),
    (False, (18, 18), (78240, 81660)),
])
def test_documented_conversion_vectors_bracket_both_quote_bounds(inverse, decimals, expected):
    with localcontext() as ctx:
        ctx.prec = 60
        low, high = Decimal('2500'), Decimal('3500')
        if inverse:
            low, high = 1 / high, 1 / low
        raw = [value * Decimal(10) ** (decimals[1] - decimals[0]) for value in (low, high)]
        spacing, base = Decimal(60), Decimal('1.0001')
        lower = int((raw[0].ln() / base.ln() / spacing).to_integral_value(rounding=ROUND_FLOOR)) * 60
        upper = int((raw[1].ln() / base.ln() / spacing).to_integral_value(rounding=ROUND_CEILING)) * 60
        assert base ** lower <= raw[0] < raw[1] <= base ** upper
        assert (lower, upper) == expected
