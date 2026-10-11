"""Amounts observed from EVM receipts, never from an aggregator quote."""
from decimal import Decimal

TRANSFER_TOPIC = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'


def native_received(conn, tx_hash, receiver):
    base=native_received_base(conn,tx_hash,receiver)
    return None if base is None else float(Decimal(base)/Decimal(10**18))


def native_received_base(conn, tx_hash, receiver):
    """Net native value from this transaction, excluding gas and other transfers.

    RPCs without callTracer leave output accounting pending. A wallet/block
    balance difference is not transaction-specific evidence.
    """
    try:
        trace = conn._rpc('debug_traceTransaction', [tx_hash, {'tracer': 'callTracer'}])
        if not isinstance(trace, dict) or not trace.get('type'):
            return None
        address = receiver.lower()

        def received(call):
            if call.get('error'):
                return 0
            total = 0
            if str(call.get('type')).upper() in {'CALL', 'CREATE', 'CREATE2', 'SELFDESTRUCT'}:
                value = call.get('value') or '0x0'
                value = int(value, 16) if isinstance(value, str) else int(value)
                if str(call.get('to') or '').lower() == address:
                    total += value
                if str(call.get('from') or '').lower() == address:
                    total -= value
            return total + sum(received(child) for child in call.get('calls') or [])

        return received(trace)
    except Exception:
        return None


def token_received(receipt, token, receiver, decimals):
    total = token_received_base(receipt, token, receiver)
    return None if total is None else float(Decimal(total)/(Decimal(10)**decimals))


def token_received_base(receipt, token, receiver):
    """Exact net ERC20 flow in raw units, including large settlement payouts."""
    if not isinstance(receipt,dict) or 'logs' not in receipt:
        return None
    total = 0
    for log in receipt['logs']:
        topics = log.get('topics') or []
        if str(log.get('address') or '').lower() != token.lower() or len(topics)<3:
            continue
        if str(topics[0]).lower() != TRANSFER_TOPIC:
            continue
        value = int(log.get('data') or '0x0',16)
        if str(topics[2])[-40:].lower() == receiver.lower().removeprefix('0x'):
            total += value
        if str(topics[1])[-40:].lower() == receiver.lower().removeprefix('0x'):
            total -= value
    return total
