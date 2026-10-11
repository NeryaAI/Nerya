"""Concentrated-liquidity units and packed v4 position metadata."""
from decimal import Decimal,localcontext

from ..contracts import FinancialError,amount


def position_ticks(info):
    if type(info) is not int or not 0<=info<2**256:raise FinancialError("invalid_v4_position_info",422)
    def signed(offset):
        value=(info>>offset)&((1<<24)-1)
        return value-(1<<24) if value&(1<<23) else value
    return signed(8),signed(32)


def token_amounts(liquidity,sqrt_price_x96,lower,upper):
    if type(lower) is not int or type(upper) is not int or not -887272<=lower<upper<=887272:
        raise FinancialError("invalid_tick_range",422)
    if type(sqrt_price_x96) is not int or not 0<sqrt_price_x96<2**160:
        raise FinancialError("invalid_pool_sqrt_price",422)
    size=amount(liquidity,zero=True)
    with localcontext() as context:
        context.prec=100
        before=Decimal("1.0001")**(Decimal(lower)/2)
        after=Decimal("1.0001")**(Decimal(upper)/2)
        current=Decimal(sqrt_price_x96)/Decimal(2**96)
        clipped=min(after,max(before,current))
        a=size*(after-clipped)/(clipped*after)
        b=size*(clipped-before)
        return max(Decimal(0),a),max(Decimal(0),b)


def pool_price(sqrt_price_x96,decimals0,decimals1):
    if type(decimals0) is not int or type(decimals1) is not int or not 0<=decimals0<=255 or not 0<=decimals1<=255:
        raise FinancialError("asset_decimals_invalid",422)
    return (Decimal(sqrt_price_x96)/Decimal(2**96))**2*Decimal(10)**(decimals0-decimals1)
