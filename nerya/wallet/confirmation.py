"""Provider-independent receipt lookup for EVM and Solana wallets."""
from .protocol import WalletSwapResult


def read_transaction(provider, config, request, transaction, *, transport=None):
    from ..connectors.evm_native import EVMNative, EVM_CHAIN_IDS
    from ..connectors.solana_native import SolanaNative
    from .receipts import token_received, native_received

    chain=request['chain']; tx_hash=transaction.get('tx_hash') or ''
    result=WalletSwapResult(provider=provider,chain=chain,ok=False,tx_hash=tx_hash,
                           amount_in=float(request['amount_in']),extra={'status':'submitted','confirmed':False})
    result.extra['transaction']=dict(transaction)
    if not tx_hash:
        result.reason='transaction hash not available; query provider execution reference'
        return result
    rpc=(config.get('rpc_urls') or {}).get(chain) or (config.get('rpc_url') if chain=='solana' else '')
    if not rpc and chain=='bsc' and provider in ('self_custody','metamask'):
        from ..connectors.bsc_native import BSCNative
        rpc=BSCNative.rpc_url
    kwargs={'transport':transport} if transport else {}
    if rpc:kwargs['rpc_url']=rpc
    owner=transaction.get('receiver') or transaction.get('owner') or request.get('receiver') or transaction.get('from') or config.get('address') or config.get('wallet_address')
    token=transaction.get('token_out') or request['token_out']
    if chain=='solana':
        conn=SolanaNative(**kwargs)
        statuses=conn._rpc('getSignatureStatuses',[[tx_hash],{'searchStatusHistory':True}])
        status=((statuses or {}).get('value') or [None])[0]
        if status and status.get('err') is not None:
            result.extra['status']='failed'; result.reason='transaction_reverted'
            return result
        if not status or status.get('confirmationStatus') not in ('confirmed','finalized'):
            return result
        source='transaction_meta'
        if token.upper() in ('SOL','NATIVE'):token='So11111111111111111111111111111111111111112'
        actual=conn.transaction_output(tx_hash,owner,token) if owner else None
        result.extra['network_fee'] = conn.transaction_fee(tx_hash, owner) if owner else {'status': 'unavailable'}
    elif chain in EVM_CHAIN_IDS:
        if not rpc:
            result.reason='configure chain RPC for independent receipt verification'
            return result
        conn=EVMNative(chain=chain,chain_id=EVM_CHAIN_IDS[chain],**kwargs)
        receipt=conn._rpc('eth_getTransactionReceipt',[tx_hash])
        if not receipt:return result
        if receipt.get('status') in ('0x0',0):
            result.extra['status']='failed'; result.reason='transaction_reverted'
            return result
        if receipt.get('status') not in ('0x1',1):
            result.reason='receipt success status unavailable'
            return result
        from .fees import evm_network_fee
        from ..connectors.chains import ChainRegistry
        result.extra['network_fee'] = evm_network_fee(receipt, tx_hash, ChainRegistry().get(chain).native_symbol)
        if transaction.get('phase')=='approval':
            result.extra['status']='failed'; result.reason='allowance_only_requires_fresh_swap_approval'
            return result
        native=transaction.get('native_out') or token.lower() in ('native','eth','bnb','matic','avax','0x'+'e'*40)
        source='transaction_trace' if native else 'receipt'
        actual=None
        if owner:
            if native:actual=native_received(conn,tx_hash,owner)
            else:
                decimals=conn.get_erc20_decimals(token)
                actual=token_received(receipt,token,owner,decimals)
    else:
        result.reason='provider must implement receipt lookup for this chain'
        return result
    result.extra['confirmed']=True
    if actual is None or actual<=0:
        result.reason='confirmed transaction; actual output evidence pending'
        return result
    result.ok=True;result.amount_out=actual
    result.extra.update(status='confirmed',amount_out_source=source)
    return result
