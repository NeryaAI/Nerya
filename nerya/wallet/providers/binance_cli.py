"""Official baw CLI read, quote and order status adapter."""
import json
import shutil
import subprocess
from pathlib import Path
from ..errors import WalletDependencyError,WalletPolicyDenied,WalletTransportError,WalletQuoteError
from ..protocol import WalletBalance,WalletQuote,WalletSwapResult


def command(provider):
    binary=provider.config.get('cli_path') or shutil.which('baw')
    if binary:return [str(binary)]
    entry=Path(provider.skill_path)/provider.entry if provider.skill_path else None
    if entry and entry.is_file() and shutil.which('node'):return ['node',str(entry)]
    raise WalletDependencyError(provider.id,['bin:baw'],'Install @binance/agentic-wallet; configure cli_path or its package entry. Docs: '+provider.repo+' / '+provider.subdir)


def call(provider,args,*,timeout_s=45):
    try:
        proc=subprocess.run(command(provider)+args+['--json'],capture_output=True,text=True,timeout=timeout_s,check=False)
    except (OSError,subprocess.TimeoutExpired) as exc:raise WalletTransportError('baw invocation failed: '+type(exc).__name__) from exc
    if proc.returncode:raise WalletTransportError(f'baw exited {proc.returncode}')
    text=proc.stdout.strip()
    try:doc=json.loads(text)
    except ValueError:
        try:doc=json.loads(text.splitlines()[-1])
        except (ValueError,IndexError) as exc:raise WalletTransportError('baw returned invalid JSON') from exc
    if not isinstance(doc,dict) or doc.get('success') is not True:raise WalletTransportError('baw request rejected')
    return doc.get('data')


def chain_id(chain):
    from ...connectors.evm_native import EVM_CHAIN_IDS
    if chain=='solana':return 'CT_501'
    if chain in EVM_CHAIN_IDS:return str(EVM_CHAIN_IDS[chain])
    raise WalletPolicyDenied('unsupported Binance chain')


def owner(provider,chain):
    doc=call(provider,['wallet','address']) or {}
    rows=doc.get('addresses') or []
    address=next((r['address'] for r in rows if str(r.get('binanceChainId'))==chain_id(chain)),None)
    expected=(provider.config.get('addresses') or {}).get(chain) or provider.config.get('wallet_address')
    if not expected:raise WalletPolicyDenied('pin Binance wallet_address/addresses before use')
    if not address or (address!=expected if chain=='solana' else address.lower()!=expected.lower()):
        raise WalletPolicyDenied('active baw account differs from pinned wallet')
    return address


def token(chain,value):
    if value.lower() in ('native','eth','bnb','sol'):
        return 'So11111111111111111111111111111111111111112' if chain=='solana' else '0x'+'e'*40
    return value


def balance(provider,chain,address,asset):
    selected=owner(provider,chain)
    if address and (address!=selected if chain=='solana' else address.lower()!=selected.lower()):raise WalletPolicyDenied('balance address mismatch')
    value=token(chain,asset)
    rows=call(provider,['wallet','balance','--tokenAddress',value,'--binanceChainId',chain_id(chain)]) or []
    matched=[r for r in rows if str(r.get('address'))==value or (chain!='solana' and str(r.get('address')).lower()==value.lower())]
    if not matched:raise WalletQuoteError('baw hides balances under $0.01; requested token balance unavailable')
    row=matched[0]
    decimals=row.get('decimals')
    if decimals is None:
        from ..token_info import token_decimals
        decimals=token_decimals(provider.config,chain,value)
    return WalletBalance(provider=provider.id,chain=chain,address=selected,token=asset,
                         balance=float(row['balance']),symbol=str(row.get('symbol') or ''),decimals=int(decimals))


def quote(provider,chain,token_in,token_out,amount_in,slippage_bps):
    owner(provider,chain)
    doc=call(provider,['market-order','quote','--fromTokenQty',str(amount_in),'--fromToken',token(chain,token_in),
        '--toToken',token(chain,token_out),'--binanceChainId',chain_id(chain),'--slippage',str(slippage_bps/100)]) or {}
    from ..adapter_contract import finite
    expected=finite(doc.get('toCoinAmount'),'toCoinAmount',positive=True)
    return WalletQuote(provider=provider.id,chain=chain,token_in=token_in,token_out=token_out,amount_in=float(amount_in),
        expected_out=expected,min_out=expected*(1-slippage_bps/10000),slippage_bps=slippage_bps,
        extra={'minimum_output':'relative_slippage','execution_requires_exact_floor_adapter':True})


def status(provider,request,transaction):
    ref=transaction.get('execution_ref')
    if ref:
        doc=call(provider,['market-order','list','--orderId',ref]) or {}
        rows=doc.get('list') or []
        row=next((r for r in rows if str(r.get('orderId'))==str(ref)),{})
        if row.get('status')=='FAILED':
            return WalletSwapResult(provider=provider.id,chain=request['chain'],ok=False,reason='provider_order_failed',extra={'status':'failed'})
        transaction={**transaction,'tx_hash':row.get('txHash') or transaction.get('tx_hash') or ''}
    from ..confirmation import read_transaction
    result=read_transaction(provider.id,provider.config,request,transaction)
    result.extra.update(execution_ref=ref,transaction=transaction)
    return result
