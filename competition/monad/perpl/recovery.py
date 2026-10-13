"""Explicit read-only remediation for bare-hex Perpl transaction IDs.

Does not mutate submitted bindings, gateway state, plugin cache or external state.
"""
from decimal import Decimal
from .trading import transaction_hash, unscaled, integer, amount
from nerya.financial.contracts import FinancialError,digest


def verify_original_fill(adapter, request, quote, submission):
    if (submission.get('chain_id')!=adapter.chain_id or submission.get('account_id')!=adapter.settings['exchange_account_id']
        or submission.get('market_id')!=quote['perpl_order']['mkt'] or quote.get('request_digest')!=digest(request)
        or quote.get('account_binding')!=adapter._binding()):
        raise FinancialError('perpl_recovery_original_binding_mismatch',403)
    adapter._account(recovery=True)
    from urllib.parse import urlencode
    page='';orders=[];seen=set()
    for _ in range(20):
        doc=adapter._request('GET','/v1/trading/order-history?'+urlencode({'count':100,**({'page':page} if page else {})}),private=True)
        orders.extend(doc.get('d',[]));page=doc.get('np')
        if not page or page in seen:break
        seen.add(page)
    matches=[o for o in orders if o.get('rq')==submission['request_id'] and o.get('acc')==submission['account_id'] and o.get('mkt')==submission['market_id']]
    if not matches:raise FinancialError('perpl_original_order_not_found',422)
    order=max(matches,key=lambda o:(o.get('at',{}).get('t',0),o.get('at',{}).get('l',0)))
    if order.get('st') not in {4,5,6,7,10} or not order.get('fs') or not order.get('fp') or 'f' not in order:
        raise FinancialError('perpl_original_terminal_fill_required',422)
    expected=quote['perpl_order']
    if any(order.get(k)!=expected.get(k) for k in ('acc','mkt','t','lv','fl')) or order.get('os')!=expected['s'] or integer(order['fs'])>expected['s']:
        raise FinancialError('perpl_original_order_shape_mismatch',403)
    tx=transaction_hash(order.get('at',{}).get('txid'))
    import json
    status,doc=adapter.http.request('POST',adapter.rpc,headers={'Content-Type':'application/json'},body=json.dumps({'jsonrpc':'2.0','id':1,'method':'eth_getTransactionReceipt','params':[tx]}).encode())
    receipt=doc.get('result') or {}
    if status!=200 or receipt.get('status')!='0x1' or receipt.get('transactionHash','').lower()!=tx:
        raise FinancialError('perpl_original_chain_receipt_not_confirmed',422)
    if int(receipt.get('blockNumber','0x0'),16)!=order['at']['b']:
        raise FinancialError('perpl_original_receipt_block_mismatch',403)
    # Perpl at.l is transaction-local; RPC logIndex is block-global. Verify the
    # original transaction's block/index and Exchange target instead of equating them.
    context=adapter._request('GET','/v1/pub/context')
    instance=next((x for x in context.get('instances',[]) if x.get('id')==adapter.settings['instance_id']),None)
    if not instance:raise FinancialError('perpl_original_instance_not_found',403)
    if receipt.get('to','').lower()!=instance['address'].lower():
        raise FinancialError('perpl_original_receipt_exchange_mismatch',403)
    if 'tx' in order['at'] and int(receipt.get('transactionIndex','0x0'),16)!=order['at']['tx']:
        raise FinancialError('perpl_original_receipt_tx_index_mismatch',403)
    exchange_logs=[log for log in receipt.get('logs',[]) if log.get('address','').lower()==instance['address'].lower()
                   and log.get('transactionHash','').lower()==tx]
    if not exchange_logs:raise FinancialError('perpl_original_receipt_exchange_logs_missing',403)
    log_index=order['at'].get('l')
    size=unscaled(order['fs'],quote['market']['config']['size_decimals'])
    price=unscaled(order['fp'],quote['market']['config']['price_decimals'])
    fee=amount(order['f'],zero=True)/10**6
    notional=size*price
    if notional>amount(quote['risk_usd'],zero=True) or fee>amount(quote['fee_usd'],zero=True):
        raise FinancialError('perpl_original_fill_exceeds_frozen_budget',403)
    return {'remediation':'bare_hex_txid_rpc_prefix','execution_confirmed':True,'gateway_state_changed':False,
        'original_submission':submission,'original_component_binding':quote.get('component_binding'),
        'original_request_digest':digest(request),'order_id':order['oid'],'request_id':order['rq'],
        'transaction_hash':tx,'block_number':order['at']['b'],'perpl_local_log_index':log_index,'rpc_exchange_log_indices':[int(log['logIndex'],16) for log in exchange_logs],
        'filled_size':str(size),'average_price':str(price),'settled_notional_usd':str(notional),
        'settled_fee_usd':str(fee),'partial':order['fs']<order['os'],'order':order}
