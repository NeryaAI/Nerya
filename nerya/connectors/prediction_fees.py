"""Read CTF Exchange v2 per-order fees from canonical Polygon receipts.

ABI: Polymarket/ctf-exchange-v2/src/exchange/mixins/Events.sol.
V2 fees are collateral amounts for BOTH BUY and SELL (Trading.sol). Never
apply V1 event layouts or estimate fees by multiplying a guessed fee rate.
"""
from decimal import Decimal
import re

from eth_abi import decode
from eth_utils import keccak

from ..core.errors import TradingError

EVENT = '0x'+keccak(text='OrderFilled(bytes32,address,address,uint8,uint256,uint256,uint256,uint256,bytes32,bytes32)').hex()


def _integer(value):
    if isinstance(value, bool):
        raise ValueError('boolean is not an integer')
    return int(value,16) if isinstance(value,str) and value.startswith('0x') else int(value)


def confirmed_order_fees(rpc, *, transactions, order_hash, owner, token_id, side, filled_shares,
                         exchange_code_hashes, confirmations=3):
    try:
        return _confirmed_order_fees(rpc,transactions=transactions,order_hash=order_hash,owner=owner,
            token_id=token_id,side=side,filled_shares=filled_shares,
            exchange_code_hashes=exchange_code_hashes,confirmations=confirmations)
    except TradingError:
        raise
    except Exception as exc:
        raise TradingError('prediction fee evidence unavailable; keep original order pending',ambiguous=True) from exc


def _confirmed_order_fees(rpc, *, transactions, order_hash, owner, token_id, side, filled_shares,
                          exchange_code_hashes, confirmations):
    hashes=list(dict.fromkeys(transactions))
    if (not hashes or len(hashes)>100 or not re.fullmatch('0x[0-9a-fA-F]{64}',order_hash)
            or not re.fullmatch('0x[0-9a-fA-F]{40}',owner) or side not in {'buy','sell'}
            or type(confirmations) is not int or not 1<=confirmations<=1000):
        raise TradingError('prediction fee verification identity invalid')
    exchanges={address.lower():code.lower() for address,code in exchange_code_hashes.items()
        if re.fullmatch('0x[0-9a-fA-F]{40}',address) and re.fullmatch('0x[0-9a-fA-F]{64}',code)}
    if not exchanges or _integer(rpc('eth_chainId',[]))!=137:
        raise TradingError('prediction reviewed Polygon fee exchange required')
    latest=_integer(rpc('eth_blockNumber',[]))
    shares=collateral=fee=0
    seen=set()
    evidence=[]
    for tx_hash in hashes:
        if not re.fullmatch('0x[0-9a-fA-F]{64}',tx_hash):
            raise TradingError('prediction settlement hash invalid')
        receipt=rpc('eth_getTransactionReceipt',[tx_hash])
        if (not isinstance(receipt,dict) or receipt.get('transactionHash','').lower()!=tx_hash.lower()
                or _integer(receipt.get('status',0))!=1):
            raise TradingError('prediction settlement receipt unavailable',ambiguous=True)
        block=receipt['blockNumber']
        canonical=rpc('eth_getBlockByNumber',[block,False])
        if not canonical or canonical.get('hash')!=receipt.get('blockHash') or latest-_integer(block)+1<confirmations:
            raise TradingError('prediction settlement not yet canonical/confirmed',ambiguous=True)
        matched=False
        checked=set()
        for log in receipt.get('logs',[]):
            topics=log.get('topics') or []
            emitter=str(log.get('address','')).lower()
            if len(topics)!=4 or topics[0].lower()!=EVENT or topics[1].lower()!=order_hash.lower():
                continue
            if emitter not in exchanges or '0x'+topics[2][-40:].lower()!=owner.lower():
                raise TradingError('prediction fill event owner or exchange mismatch')
            if emitter not in checked:
                code=rpc('eth_getCode',[emitter,block])
                if not code or '0x'+keccak(bytes.fromhex(code[2:])).hex()!=exchanges[emitter]:
                    raise TradingError('prediction fee exchange code changed')
                checked.add(emitter)
            if log.get('removed') is True or log.get('logIndex') is None:
                raise TradingError('prediction fill event index unavailable')
            identity=(tx_hash.lower(),_integer(log['logIndex']))
            if identity in seen:
                continue
            seen.add(identity)
            data=bytes.fromhex(log['data'][2:])
            if len(data)!=224:
                raise TradingError('prediction fee event ABI mismatch')
            event_side,event_token,making,taking,paid,_,_=decode(['uint8','uint256','uint256','uint256','uint256','bytes32','bytes32'],data)
            if event_side!=(0 if side=='buy' else 1) or event_token!=int(token_id):
                raise TradingError('prediction fill event token or side mismatch')
            quantity,notional=(taking,making) if side=='buy' else (making,taking)
            if quantity<=0 or notional<=0:
                raise TradingError('prediction fill event amounts invalid')
            matched=True
            shares+=quantity
            collateral+=notional
            fee+=paid
            evidence.append({'transaction_hash':tx_hash,'log_index':identity[1],'exchange':emitter,
                             'shares_base':str(quantity),'collateral_base':str(notional),'fee_base':str(paid)})
        if not matched:
            raise TradingError('prediction order fee event not found; do not assume zero',ambiguous=True)
    if abs(Decimal(shares)/1_000_000-Decimal(str(filled_shares)))>Decimal('.000001'):
        raise TradingError('prediction chain/API filled quantity mismatch',ambiguous=True)
    return {'status':'chain_verified','fee_asset':'PUSD','fee_base':str(fee),'fee_collateral':str(Decimal(fee)/1_000_000),
            'cumulative_notional':str(Decimal(collateral)/1_000_000),'evidence':evidence,
            'valuation_basis':'nominal collateral, not a PUSD/USD conversion'}
