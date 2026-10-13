"""Operator-side public data adapters for StrategyRunner.news_fetchers.

Not part of strategy packages. GET and explicitly allowlisted eth_* reads only.
No config/vault/private endpoints/signing/broadcast. A failure yields no signal.
"""
from decimal import Decimal, ROUND_DOWN, localcontext
import hashlib
import json
from pathlib import Path
import time
from urllib.request import Request, build_opener, HTTPRedirectHandler

from eth_abi import decode
from eth_utils import keccak
from nerya.financial.defi.liquidity import token_amounts, pool_price
try:
    from .configure import deployment
except ImportError:
    from configure import deployment

API = 'https://testnet.perpl.xyz/api'
RPC = 'https://testnet-rpc.monad.xyz'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Public reader redirect denied')


def read(url, body=None):
    if not (url == RPC or url.startswith(API+'/v1/pub/') or url.startswith(API+'/v1/market-data/')):
        raise ValueError('Testnet public endpoint required')
    request = Request(url, data=json.dumps(body).encode() if body else None,
        headers={'Content-Type': 'application/json'} if body else {}, method='POST' if body else 'GET')
    with build_opener(NoRedirect()).open(request, timeout=15) as response:
        return json.loads(response.read())


def rpc(method, params):
    if method not in {'eth_chainId', 'eth_getBlockByNumber', 'eth_getCode', 'eth_call'}:
        raise ValueError('Public read-only RPC method required')
    doc = read(RPC, {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})
    if doc.get('error') or 'result' not in doc:
        raise ValueError('RPC read failed')
    return doc['result']


def chain():
    if rpc('eth_chainId', []) != hex(10143):
        raise ValueError('Monad testnet chain identity mismatch')


def call(target, signature, types, block):
    data = '0x'+keccak(text=signature)[:4].hex()
    return decode(types, bytes.fromhex(rpc('eth_call', [{'to': target, 'data': data}, block])[2:]))


def pool_snapshot(manifest, deployment_path):
    p = manifest['params']
    doc, sha = deployment(deployment_path)
    if sha != p['deployment_sha256'] or doc['pools'][p['pool_id']] != p['pool']:
        raise ValueError('Installed package/deployment changed')
    chain()
    block = rpc('eth_getBlockByNumber', ['latest', False])
    at = int(block['timestamp'], 16)*1000
    if not 0 <= int(time.time()*1000)-at <= p['max_data_age_seconds']*1000:
        raise ValueError('Public pool block stale')
    pool = p['pool']
    for target, expected in [(pool['address'], pool['code_hash']), *[(token, doc['tokens'][token.lower()]['code_hash']) for token in (pool['token0'],pool['token1'])]]:
        code = rpc('eth_getCode', [target, block['number']])
        if code == '0x' or '0x'+keccak(bytes.fromhex(code[2:])).hex() != expected:
            raise ValueError('Reviewed pool/token bytecode changed')
    for field, signature, types in [('token0','token0()',['address']), ('token1','token1()',['address']), ('fee','fee()',['uint24']), ('tick_spacing','tickSpacing()',['int24'])]:
        value = call(pool['address'],signature,types,block['number'])[0]
        if str(value).lower() != str(pool[field]).lower():
            raise ValueError('Pool identity changed')
    sqrt, tick, *_ = call(pool['address'], 'slot0()', ['uint160','int24','uint16','uint16','uint16','uint8','bool'], block['number'])
    decimals = [call(token,'decimals()',['uint8'],block['number'])[0] for token in (pool['token0'], pool['token1'])]
    if decimals != [doc['tokens'][token.lower()]['decimals'] for token in (pool['token0'],pool['token1'])]:
        raise ValueError('Token decimals changed')
    row = {'id': block['hash']+':'+p['pool_id'], 'kind': 'pool', 'chain_id': 10143, 'deployment_sha256': sha,
        'source': RPC, 'as_of_ms': at, 'block': int(block['number'],16), 'block_hash': block['hash'],
        **{key: pool[key] for key in ('address','token0','token1','fee','tick_spacing')}, 'pool_id': p['pool_id'],
        'tick': tick, 'sqrt_price_x96': str(sqrt), 'decimals': decimals}
    with localcontext() as context:
        context.prec = 100
        price = pool_price(sqrt,*decimals)
        if 'test_token' in p:
            row['quote_per_token'] = str(price if p['test_token'].lower()==pool['token0'].lower() else 1/price)
        if 'range_ticks' in p:
            center = tick//pool['tick_spacing']*pool['tick_spacing']
            lower, upper = center-p['range_ticks'],center+p['range_ticks']
            unit = token_amounts(1,sqrt,lower,upper)
            cap = [Decimal(p[f'amount{i}_max'])*10**decimals[i] for i in (0,1)]
            liquidity = int(min(c/u for c,u in zip(cap,unit)) * Decimal('0.99'))
            quantities = token_amounts(liquidity,sqrt,lower,upper)
            row.update(range_ticks=p['range_ticks'], tick_lower=lower, tick_upper=upper,
                mint_amounts=[str(Decimal(int(q))/10**d) for q,d in zip(quantities,decimals)])
    return row


def perpl_snapshot():
    chain()
    context = read(API+'/v1/pub/context')
    if context.get('chain',{}).get('chain_id') != 10143:
        raise ValueError('Perpl API network mismatch')
    market = next((m for m in context['markets'] if m.get('size_units')=='MON' or m.get('name','').removesuffix(' Perp')=='MON'),None)
    if not market or not market['config']['is_open']:
        raise ValueError('MON market unavailable')
    now = int(time.time()*1000)
    at = market['state']['at']['t']
    if not 0 <= now-at <= 30000:
        raise ValueError('Perpl mark stale')
    end = now//3600000*3600000
    endpoint = API+f"/v1/market-data/{market['id']}/candles/3600/{end-72*3600000}-{end-1}"
    data = read(endpoint)
    divisor = Decimal(10)**market['config']['price_decimals']
    return {'id': 'perpl:'+str(at), 'kind': 'perpl', 'chain_id': 10143, 'market': 'PERPL:MON/AUSD',
        'as_of_ms': at, 'source': endpoint, 'context_source': API+'/v1/pub/context', 'mark': str(Decimal(market['state']['mrk'])/divisor),
        'market_id': market['id'], 'candles': [{'time': c['t'], 'close': str(Decimal(c['c'])/divisor)} for c in data['d'] if c['t']+3600000<=now]}


def fetchers(manifests, deployment_path):
    """Pass this map to the real native runner; each tick pulls fresh public state."""
    output = {}
    for manifest in manifests:
        p = manifest['params']
        source = p['observation_source']
        if source == 'monad_perp_public':
            if (type(p.get('chain_id')) is not int or p['chain_id'] != 10143
                    or p.get('chain') != 'monad-testnet'
                    or manifest.get('markets') != ['PERPL:MON/AUSD']
                    or not isinstance(p.get('account_id'), str) or not p['account_id'].strip()
                    or manifest.get('accounts') != [p['account_id']]
                    or source not in manifest.get('news_sources', [])):
                raise ValueError('Perpl public reader testnet market/account/source binding required')
            def fetch(*, since=None, limit=1):
                row = perpl_snapshot()
                if row.get('chain_id') != 10143 or row.get('market') != 'PERPL:MON/AUSD':
                    raise ValueError('Perpl public observation identity mismatch')
                return [row]
        elif source in {'monad_lp_public', 'monad_meme_public'}:
            def fetch(*, since=None, limit=1, manifest=manifest):
                return [pool_snapshot(manifest, deployment_path)]
        else:
            raise ValueError('Unsupported Monad public observation source')
        if source in output:
            raise ValueError('Duplicate Monad public observation source')
        output[source] = fetch
    return output
