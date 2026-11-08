"""Counterfactual fixed-range v3 accounting, denominated in token1 units.

Historical fee growth belongs to actual pool liquidity. Applying it to a
counterfactual position ignores that position's dilution and swap impact; this
module establishes modeled balances, never fills, realized fees or USD value.
The caller owns attested observations, source binding and coverage.
Tick boundaries and liquidity sizing use exact integers/rationals; principal
amounts are continuous modeled balances rounded to 512 significant digits,
not the protocol's integer mint/burn transfers. Observations retain fractional
fee accrual; explicit collect/remove checkpoint it to whole base units.
"""
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from fractions import Fraction

from .....financial.defi.liquidity import pool_price


UINT256 = 2**256
Q128 = 2**128
MIN_SQRT_RATIO = 4295128739
MAX_SQRT_RATIO = 1461446703485210103287273052203988822378723970342
# Bound fractional input scale as well as magnitude; leave ample precision for
# base-unit conversion, principal math and token1 valuation across those bounds.
PRECISION = 512
_CONTEXT = Context(prec=PRECISION, rounding=ROUND_HALF_EVEN, Emin=-999999, Emax=999999)


def _uint(value, bits=256):
    if type(value) is str and value.isascii() and value.isdigit() and len(value) <= 78:
        value = int(value)
    if type(value) is not int or not 0 <= value < 2**bits:
        raise ValueError('LP unsigned integer outside bounds')
    return value


def _decimal(value):
    if type(value) not in (str, int, Decimal) or isinstance(value, bool):
        raise ValueError('LP amounts require exact decimal strings or integers')
    if len(str(value)) > 120:
        raise ValueError('LP amount exceeds bounds')
    try:
        number = Decimal(value)
    except Exception as exc:
        raise ValueError('LP invalid decimal amount') from exc
    if not number.is_finite() or number < 0 or number > Decimal('1e60') or number.as_tuple().exponent < -120:
        raise ValueError('LP amount outside bounds')
    return number


def fee_growth_inside(*, tick, lower, upper, global_growth, lower_outside, upper_outside):
    """v3 modulo-uint256 growth; a wrap count cannot be inferred from samples."""
    if any(type(v) is not int for v in (tick, lower, upper)) or not -887272 <= lower < upper <= 887272 or not -887272 <= tick <= 887272:
        raise ValueError('LP invalid ticks')
    global_growth, lower_outside, upper_outside = map(_uint, (global_growth, lower_outside, upper_outside))
    below = lower_outside if tick >= lower else (global_growth - lower_outside) % UINT256
    above = upper_outside if tick < upper else (global_growth - upper_outside) % UINT256
    return (global_growth - below - above) % UINT256


def _sqrt_ratio_at_tick(tick):
    """v3 TickMath: Q128.128 reciprocal products, rounded up to Q64.96."""
    factors = (
        0xfffcb933bd6fad37aa2d162d1a594001, 0xfff97272373d413259a46990580e213a,
        0xfff2e50f5f656932ef12357cf3c7fdcc, 0xffe5caca7e10e4e61c3624eaa0941cd0,
        0xffcb9843d60f6159c9db58835c926644, 0xff973b41fa98c081472e6896dfb254c0,
        0xff2ea16466c96a3843ec78b326b52861, 0xfe5dee046a99a2a811c461f1969c3053,
        0xfcbe86c7900a88aedcffc83b479aa3a4, 0xf987a7253ac413176f2b074cf7815e54,
        0xf3392b0822b70005940c7a398e4b70f3, 0xe7159475a2c29b7443b29c7fa6e889d9,
        0xd097f3bdfd2022b8845ad8f792aa5825, 0xa9f746462d870fdf8a65dc1f90e061e5,
        0x70d869a156d2a1b890bb3df62baf32f7, 0x31be135f97d08fd981231505542fcfa6,
        0x9aa508b5b7a84e1c677de54f3e99bc9, 0x5d6af8dedb81196699c329225ee604,
        0x2216e584f5fa1ea926041bedfe98, 0x48a170391f7dc42444e8fa2,
    )
    ratio = Q128
    for bit, factor in enumerate(factors):
        if abs(tick) & (1 << bit):
            ratio = ratio * factor >> 128
    if tick > 0:
        ratio = (UINT256 - 1) // ratio
    return (ratio + 2**32 - 1) >> 32


def _amount_ratios(sqrt, lower, upper):
    before, after = _sqrt_ratio_at_tick(lower), _sqrt_ratio_at_tick(upper)
    clipped = min(after, max(before, sqrt))
    return (Fraction(2**96 * (after - clipped), clipped * after),
            Fraction(clipped - before, 2**96))


def _token_amounts(liquidity, sqrt, lower, upper):
    return [Decimal(liquidity * ratio.numerator) / ratio.denominator
            for ratio in _amount_ratios(sqrt, lower, upper)]


class ConcentratedLiquidityLedger:
    """No swaps: enter only with available token inventory; explicit costs."""

    def __init__(self, *, token0_units, token1_units, decimals, lower, upper, action_cost_token1):
        if not isinstance(decimals, (list, tuple)) or len(decimals) != 2 or any(type(v) is not int or not 0 <= v <= 36 for v in decimals):
            raise ValueError('LP explicit token decimals required')
        fee_growth_inside(tick=lower, lower=lower, upper=upper, global_growth=0, lower_outside=0, upper_outside=0)
        self.decimals, self.lower, self.upper = tuple(decimals), lower, upper
        self.balances = [_decimal(token0_units), _decimal(token1_units)]
        self.cost = _decimal(action_cost_token1)
        self.liquidity = 0
        self.owed_base = [0, 0]
        self.fee_remainder = [0, 0]
        self.last_growth = None
        self.last_block = None

    def observe(self, *, block_number, sqrt_price_x96, tick, global_growth, lower_outside, upper_outside, initialized):
        if type(block_number) is not int or block_number < 0 or self.last_block is not None and block_number <= self.last_block:
            raise ValueError('LP observations require increasing historical blocks')
        sqrt = _uint(sqrt_price_x96, 160)
        if not MIN_SQRT_RATIO <= sqrt < MAX_SQRT_RATIO or not isinstance(initialized, (tuple, list)) or len(initialized) != 2 or any(type(v) is not bool for v in initialized):
            raise ValueError('LP invalid historical range state')
        if self.liquidity and not all(initialized):
            raise ValueError('LP historical range not initialized; fee growth is unavailable')
        if any(not isinstance(v, (tuple, list)) or len(v) != 2 for v in (global_growth, lower_outside, upper_outside)):
            raise ValueError('LP two token growth counters required')
        growth = [fee_growth_inside(tick=tick, lower=self.lower, upper=self.upper,
            global_growth=global_growth[i], lower_outside=lower_outside[i], upper_outside=upper_outside[i]) for i in (0, 1)]
        # A downward crossing can leave slot0.tick one below the exact boundary.
        if tick == 887272 or not _sqrt_ratio_at_tick(tick) <= sqrt <= _sqrt_ratio_at_tick(tick + 1):
            raise ValueError('LP historical tick and price disagree')
        if self.liquidity and self.last_growth is not None:
            for i in (0, 1):
                # Observations are not position updates: retain sub-base-unit
                # accrual until an explicit collect/remove checkpoints fees.
                earned, self.fee_remainder[i] = divmod(
                    ((growth[i] - self.last_growth[i]) % UINT256) * self.liquidity
                    + self.fee_remainder[i], Q128)
                self.owed_base[i] += earned
        self.last_growth, self.last_block = growth, block_number
        self.sqrt, self.tick, self.initialized = sqrt, tick, tuple(initialized)
        return self.snapshot()

    def _require_observation(self):
        if self.last_block is None:
            raise ValueError('LP historical observation required before action')

    def enter(self):
        self._require_observation()
        if self.liquidity or any(self.owed_base) or not all(self.initialized):
            raise ValueError('LP entry requires empty position and observed initialized boundaries')
        if self.balances[1] < self.cost:
            raise ValueError('LP insufficient token1 for action cost')
        with localcontext(_CONTEXT):
            per_unit = _amount_ratios(self.sqrt, self.lower, self.upper)
            available = [self.balances[0] * 10**self.decimals[0], (self.balances[1] - self.cost) * 10**self.decimals[1]]
            capacity = [Fraction(available[i]) // per_unit[i] for i in (0, 1) if per_unit[i] > 0]
            size = min(capacity) if capacity else 0
            if not 0 < size < 2**128:
                raise ValueError('LP available inventory cannot fund bounded liquidity')
            used = _token_amounts(size, self.sqrt, self.lower, self.upper)
            next_balances = [self.balances[i] - used[i] / 10**self.decimals[i] for i in (0, 1)]
            next_balances[1] -= self.cost
            if any(v < 0 for v in next_balances):
                raise ValueError('LP entry exceeds inventory')
            self.balances, self.liquidity = next_balances, size
        return self.snapshot()

    def collect(self):
        self._require_observation()
        with localcontext(_CONTEXT):
            balances = [self.balances[i] + Decimal(self.owed_base[i]) / 10**self.decimals[i] for i in (0, 1)]
            if balances[1] < self.cost:
                raise ValueError('LP insufficient token1 for action cost')
            balances[1] -= self.cost
            self.balances, self.owed_base = balances, [0, 0]
            self.fee_remainder = [0, 0]
        return self.snapshot()

    def remove(self):
        self._require_observation()
        if not self.liquidity:
            raise ValueError('LP no modeled liquidity to remove')
        with localcontext(_CONTEXT):
            amounts = _token_amounts(self.liquidity, self.sqrt, self.lower, self.upper)
            balances = [self.balances[i] + (amounts[i] + self.owed_base[i]) / 10**self.decimals[i] for i in (0, 1)]
            if balances[1] < self.cost:
                raise ValueError('LP insufficient token1 for action cost')
            balances[1] -= self.cost
            self.balances, self.liquidity, self.owed_base = balances, 0, [0, 0]
            self.fee_remainder = [0, 0]
        return self.snapshot()

    def snapshot(self):
        self._require_observation()
        with localcontext(_CONTEXT):
            price = pool_price(self.sqrt, *self.decimals)
            amounts = _token_amounts(self.liquidity, self.sqrt, self.lower, self.upper)
            principal = [amounts[i] / 10**self.decimals[i] for i in (0, 1)]
            fees = [Decimal(self.owed_base[i]) / 10**self.decimals[i] for i in (0, 1)]
            principal_value = principal[0] * price + principal[1]
            fee_value = fees[0] * price + fees[1]
            nav = self.balances[0] * price + self.balances[1] + principal_value + fee_value
            return {'block_number': self.last_block, 'liquidity': str(self.liquidity),
                'cash_units': [str(v) for v in self.balances], 'principal_units': [str(v) for v in principal],
                'owed_base': [str(v) for v in self.owed_base], 'price_token1': str(price),
                'principal_value_token1': str(principal_value), 'fees_value_token1': str(fee_value),
                'modeled_value_token1': str(nav), 'performance_evidence': False,
                'financial_execution_allowed': False}
