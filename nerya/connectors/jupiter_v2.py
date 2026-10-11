"""Jupiter Swap API v2 with fixed spend/slippage and durable send identity.

The initial supported path is an owned fee payer with one signer. RFQ and
sponsored multi-signer transactions are explicitly excluded, not blind-signed.
https://developers.jup.ag/docs/api-reference/swap/order
"""
import base64
from decimal import Decimal, InvalidOperation

from ..core.errors import TradingError


def integer(value, name, *, minimum=0):
    try:
        number = Decimal(str(value))
        if isinstance(value, bool) or not number.is_finite() or number != number.to_integral_value() or not minimum <= number <= 2**64 - 1:
            raise ValueError
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise TradingError(f"Jupiter invalid {name}") from exc
    return int(number)


def validate_quote(conn, doc, *, input_mint, output_mint, amount, slippage, taker=None):
    if not isinstance(doc, dict) or doc.get('error') or doc.get('errorCode'):
        raise TradingError('Jupiter v2 quote unavailable')
    if (doc.get('inputMint'), doc.get('outputMint'), integer(doc.get('inAmount'), 'input amount', minimum=1)) != (input_mint, output_mint, amount):
        raise TradingError('Jupiter quote asset/amount mismatch')
    output = integer(doc.get('outAmount'), 'output amount', minimum=1)
    floor = integer(doc.get('otherAmountThreshold'), 'minimum output', minimum=1)
    actual_slippage = integer(doc.get('slippageBps'), 'slippage')
    if doc.get('swapMode') != 'ExactIn' or actual_slippage > slippage or not output >= floor >= output * (10000 - slippage) // 10000:
        raise TradingError('Jupiter quote exceeds approved slippage')
    if doc.get('router') not in {'metis', 'dflow', 'okx'}:
        raise TradingError('Jupiter router requires a different signer model')
    if taker:
        if doc.get('taker') != taker or doc.get('signatureFeePayer') != taker or doc.get('gasless') is not False:
            raise TradingError('Jupiter sponsored or foreign fee payer is unsupported')
        fee = sum(integer(doc.get(key), key) for key in ('signatureFeeLamports', 'prioritizationFeeLamports'))
        rent = integer(doc.get('rentFeeLamports'), 'rent fee')
        if fee > conn.jupiter_max_total_fee_lamports or rent > conn.jupiter_max_rent_lamports:
            raise TradingError('Jupiter fee/rent exceeds configured cap')
        if integer(doc.get('prioritizationFeeLamports'), 'priority fee') > conn.jupiter_max_priority_fee_lamports:
            raise TradingError('Jupiter priority fee exceeds configured cap')
    return doc


def quote(conn, *, input_mint, output_mint, amount_in_raw, slippage_bps, taker=None, only_direct_routes=False):
    if not conn.jupiter_api_key:
        raise TradingError('Jupiter v2 requires a configured vault-backed API key')
    amount = integer(amount_in_raw, 'input amount', minimum=1)
    slippage = integer(slippage_bps, 'slippage')
    if slippage > 10000 or only_direct_routes:
        raise TradingError('Jupiter v2 order does not support the requested routing/slippage')
    params = {'inputMint': input_mint, 'outputMint': output_mint, 'amount': str(amount),
              'swapMode': 'ExactIn', 'slippageBps': str(slippage), 'excludeRouters': 'jupiterz',
              'priorityFeeLamports': str(conn.jupiter_max_priority_fee_lamports),
              'jitoTipLamports': '0', 'broadcastFeeType': 'maxCap'}
    if taker:
        params['taker'] = taker
    if conn.jupiter_exclude_dexes:
        params['excludeDexes'] = ','.join(conn.jupiter_exclude_dexes)
        # DEX exclusions only constrain Metis, not the competing routers.
        params['excludeRouters'] = 'jupiterz,dflow,okx'
    status, doc = conn.transport.request('GET', conn.jupiter_url.rstrip('/') + '/order', params=params,
        headers={'x-api-key': conn.jupiter_api_key}, timeout=15.0)
    if status != 200:
        raise TradingError(f'Jupiter v2 order HTTP {status}')
    if conn.jupiter_exclude_dexes and (not isinstance(doc, dict) or doc.get('router') != 'metis'):
        raise TradingError('Jupiter routing exclusions were not honoured')
    return validate_quote(conn, doc, input_mint=input_mint, output_mint=output_mint,
                          amount=amount, slippage=slippage, taker=taker)


def execute(conn, doc, key, *, input_mint, output_mint, amount, slippage, owner, confirm=True, on_broadcast=None):
    from .solana_native import _read_shortvec_u16, _sign_solana_v0_tx
    import base58

    conn._check_live()
    validate_quote(conn, doc, input_mint=input_mint, output_mint=output_mint,
                   amount=amount, slippage=slippage, taker=owner)
    if not doc.get('transaction') or not isinstance(doc.get('requestId'), str) or not doc['requestId']:
        raise TradingError('Jupiter returned a quote without an executable transaction')
    try:
        raw = base64.b64decode(doc['transaction'], validate=True)
    except (ValueError, TypeError) as exc:
        raise TradingError('Jupiter transaction encoding invalid') from exc
    count, _ = _read_shortvec_u16(raw, 0)
    if count != 1:
        raise TradingError('Jupiter transaction requires unsupported additional signers')
    height = integer(doc.get('lastValidBlockHeight'), 'last valid block height', minimum=1)
    if integer(conn._rpc('getBlockHeight', [{'commitment': 'confirmed'}]), 'block height') > height:
        raise TradingError('Jupiter transaction expired before submission')
    signed = _sign_solana_v0_tx(doc['transaction'], key)
    # Simulation is read-only and must succeed before anything is submitted.
    simulated = conn._rpc('simulateTransaction', [signed, {'encoding': 'base64', 'sigVerify': True,
                                                        'commitment': 'confirmed'}])
    value = simulated.get('value') if isinstance(simulated, dict) else None
    if not isinstance(value, dict) or 'err' not in value or value['err'] is not None:
        raise TradingError('Jupiter transaction simulation failed')
    raw = base64.b64decode(signed)
    _, offset = _read_shortvec_u16(raw, 0)
    signature = base58.b58encode(raw[offset:offset + 64]).decode()
    reference = {'tx_hash': signature, 'chain': 'solana', 'owner': owner, 'token_out': output_mint,
                 'jupiter_request_id': doc['requestId'], 'last_valid_block_height': height}
    if on_broadcast:
        on_broadcast(reference)  # Persist before POST, including a timeout/lost response.
    try:
        status, response = conn.transport.request('POST', conn.jupiter_url.rstrip('/') + '/execute',
            body={'signedTransaction': signed, 'requestId': doc['requestId'], 'lastValidBlockHeight': height},
            headers={'x-api-key': conn.jupiter_api_key}, timeout=30.0)
    except Exception as exc:
        raise TradingError('Jupiter submit outcome unknown; reconcile the persisted signature', ambiguous=True) from exc
    if status != 200 or not isinstance(response, dict) or response.get('signature') != signature:
        raise TradingError('Jupiter submit response unverified; reconcile the persisted signature', ambiguous=True)
    result = {'signature': signature, 'user': owner, 'confirmed': False, 'quote': doc,
              'jupiter_request_id': doc['requestId'], 'output_mint': output_mint}
    if confirm:
        try:
            observed = conn.wait_for_signature(signature)
            result.update(confirmed=True, confirmation_status=observed.get('confirmationStatus'), slot=observed.get('slot'),
                          amount_out=conn.transaction_output(signature, owner, output_mint))
            result['network_fee'] = conn.transaction_fee(signature, owner)
        except TradingError as exc:
            result['confirmation_error'] = str(exc)
    # Never use an API status or quote amount as proof of a chain fill.
    return result
