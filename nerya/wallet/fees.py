"""Receipt-derived network costs. Swap outputs are already net of routing fees.

Keep unavailable prices explicit. A confirmed fill must still be recorded even
when a valuation service is down; it must not be advertised as net-of-all-costs.
"""
from decimal import Decimal, InvalidOperation
import time


def evm_network_fee(receipt, transaction_hash, symbol):
    if not isinstance(receipt, dict) or receipt.get('gasUsed') is None or receipt.get('effectiveGasPrice') is None:
        return {'status': 'unavailable'}
    try:
        def integer(value):
            if isinstance(value, bool):
                raise ValueError
            return int(value, 16) if isinstance(value, str) and value.startswith('0x') else int(value)
        gas, price = integer(receipt['gasUsed']), integer(receipt['effectiveGasPrice'])
        if gas < 0 or price < 0:
            raise ValueError
        fee = gas * price
        # OP-stack receipts expose L1 data costs separately. Never silently
        # discard them; other rollups may need a dedicated fee model.
        if receipt.get('l1Fee') is not None:
            l1 = integer(receipt['l1Fee'])
            if l1 < 0:
                raise ValueError
            fee += l1
    except (ValueError, TypeError, OverflowError):
        return {'status': 'unavailable'}
    return {'status': 'observed', 'asset': symbol, 'amount_base': str(fee), 'decimals': 18,
            'transaction_hash': transaction_hash, 'source': 'receipt', 'l1_fee_included': receipt.get('l1Fee') is not None}


def price_network_fee(evidence, *, price_provider=None):
    if not isinstance(evidence, dict) or evidence.get('status') != 'observed':
        return {'status': 'unpriced_gas', 'network_fee': evidence or {'status': 'unavailable'}}
    try:
        value = Decimal(str(evidence['amount_base']))
        decimals = evidence['decimals']
        if (isinstance(evidence['amount_base'], bool) or not value.is_finite() or value < 0
                or value != value.to_integral_value() or type(decimals) is not int or not 0 <= decimals <= 255):
            raise ValueError
        native_amount = value / Decimal(10) ** decimals
        if not value:
            return {'status': 'verified', 'network_fee': evidence, 'fee_usd': 0.0,
                    'valuation_source': 'observed_zero', 'as_of': time.time()}
        if price_provider is None:
            from ..financial.adapters import usd_price
            price_provider = usd_price
        price = Decimal(str(price_provider(evidence['asset'])))
        if not price.is_finite() or price <= 0:
            raise ValueError
        return {'status': 'verified', 'network_fee': evidence, 'fee_usd': float(native_amount * price),
                'price_usd': str(price), 'valuation_source': 'valuation_time_quote', 'as_of': time.time()}
    except (KeyError, ValueError, TypeError, InvalidOperation, ArithmeticError):
        return {'status': 'unpriced_gas', 'network_fee': evidence}
    except Exception:
        # Network/valuation failure cannot erase an already-confirmed fill.
        return {'status': 'unpriced_gas', 'network_fee': evidence, 'reason': 'fee_valuation_unavailable'}
