"""Transaction-specific destination credit. Never infer credit from prose."""
from .contracts import FinancialError
from .adapters import units,validate_evm_address


def verify_credit(settings,*,receiver,transaction_hash,minimum_base,transport=None):
    chain=settings.get('chain');token=settings.get('token');rpc=settings.get('rpc_url')
    if not rpc or not token:raise FinancialError('destination_verifier_unconfigured',422)
    kwargs={'rpc_url':rpc}
    if transport is not None:kwargs['transport']=transport
    if chain=='solana':
        from ..connectors.solana_native import SolanaNative
        conn=SolanaNative(**kwargs)
        status=conn._rpc('getSignatureStatuses',[[transaction_hash],{'searchTransactionHistory':True}])['value'][0]
        if not status or status.get('confirmationStatus')!='finalized':return {'confirmed':False}
        if status.get('err') is not None:return {'confirmed':False,'failed':True}
        tx=conn._rpc('getTransaction',[transaction_hash,{'encoding':'jsonParsed','commitment':'finalized','maxSupportedTransactionVersion':0}])
        if not tx or (tx.get('meta') or {}).get('err') is not None:return {'confirmed':False,'failed':True}
        if transaction_hash not in tx['transaction'].get('signatures',[]):return {'confirmed':False,'failed':True}
        meta=tx['meta']
        if token=='NATIVE':
            keys=[key.get('pubkey') if isinstance(key,dict) else key for key in tx['transaction']['message']['accountKeys']]
            if receiver not in keys:return {'confirmed':False,'failed':True}
            index=keys.index(receiver);credited=meta['postBalances'][index]-meta['preBalances'][index]
        else:
            before={row['accountIndex']:int(row['uiTokenAmount']['amount']) for row in meta.get('preTokenBalances',[]) if row.get('owner')==receiver and row.get('mint')==token}
            credited=sum(int(row['uiTokenAmount']['amount'])-before.get(row['accountIndex'],0) for row in meta.get('postTokenBalances',[]) if row.get('owner')==receiver and row.get('mint')==token)
    else:
        from ..connectors.evm_native import EVMNative,EVM_CHAIN_IDS
        if chain not in EVM_CHAIN_IDS:raise FinancialError('destination_chain_unsupported',422)
        conn=EVMNative(chain=chain,chain_id=EVM_CHAIN_IDS[chain],**kwargs);conn._verify_chain_id()
        receipt=conn._rpc('eth_getTransactionReceipt',[transaction_hash])
        if not receipt:return {'confirmed':False}
        if int(receipt.get('status','0x0'),16)!=1:return {'confirmed':False,'failed':True}
        if conn.get_block_number()-int(receipt['blockNumber'],16)+1<int(settings.get('confirmations',12)):return {'confirmed':False}
        if token=='NATIVE':
            from ..wallet.receipts import native_received_base
            observed=native_received_base(conn,transaction_hash,receiver)
            if observed is None:return {'confirmed':False,'reason':'native_credit_trace_unavailable'}
            credited=observed
        else:
            from ..wallet.receipts import TRANSFER_TOPIC
            validate_evm_address(token);validate_evm_address(receiver)
            credited=0
            for log in receipt.get('logs',[]):
                topics=log.get('topics',[])
                if log.get('address','').lower()!=token.lower() or len(topics)<3 or topics[0].lower()!=TRANSFER_TOPIC:continue
                quantity=int(log.get('data','0x0'),16)
                if topics[2][-40:].lower()==receiver[2:].lower():credited+=quantity
                if topics[1][-40:].lower()==receiver[2:].lower():credited-=quantity
    return {'confirmed':credited>=int(minimum_base),'credit_base':str(credited),'transaction_hash':transaction_hash,
        'reason':None if credited>=int(minimum_base) else 'destination_credit_below_minimum'}
