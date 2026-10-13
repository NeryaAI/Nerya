#!/usr/bin/env python3
"""Operator-only canonical UniV3 testnet bootstrap. No strategy execution here."""
import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import time
from decimal import Decimal
from urllib.parse import urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener

from eth_abi import encode, decode
from eth_account import Account
from eth_utils import keccak, to_checksum_address

ROOT = Path(__file__).resolve().parent
CHAIN_ID = 10143
WMON = '0xFb8bf4c1CC7a94c73D209a149eA2AbEa852BC541'
WMON_CODE_HASH = '0xa32df4f1226f1bb0d6ce9b917752ed687857899a35ebf902c9992edb73b138b6'
DEFAULT_RPC = 'https://testnet-rpc.monad.xyz'
ALLOWED_RPC = {DEFAULT_RPC, DEFAULT_RPC + '/'}
EXPECTED_REVISION = '@uniswap/v3-core@1.0.1;@uniswap/v3-periphery@1.4.4;solc@0.8.28'
EXPECTED_SENDER = '0x6BB4380Bb2aD6dbA67cAb898dFFa2ED2D4c9E5fe'
PINNED_CREATION_SHA256 = {
    'UniswapV3Factory': '62013628a11b8fa975d023551100c633363a6380a820476e86f6ceb1aeb08b1e',
    'UniswapV3Pool': 'f8165e94f91d71d983d5453de1d50437c070acfe59a24b78e42c447744dee98b',
    'SwapRouter': '48fc4c6a8dcaa40a6c8eaffaa80813fbdc1f61eeed2820e89c91d9a4c74a6358',
    'QuoterV2': 'fb92a70bd2439642d17c69cf7bc0a532f0d19aa2fc9773da7c3764c8bf4e6e16',
    'NonfungiblePositionManager': 'f803985e92b60a4a49c6cf0628a10ef8105bc812ff98cf13aa5af4a133a72052',
    'FixedTestPriceFeed': '1c74b5f0c0a063ed490f4f5ed99cf29c48cc06813b75037ca5273757eb9c7ba0',
    'TestPositionDescriptor': 'f51c5f6e58a2951c1c2ddef22c68fc57929b54c433ef9e3e2dfa0b24e9404cf2',
    'TestToken': '337292f843f26a189fc9f3b1145084d3cd2b183b2ee7645699cdea1a260d9925',
}
Q96 = 2**96
ZERO = '0x' + '00'*20


def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def call_data(signature, types=(), values=()):
    return '0x' + (keccak(text=signature)[:4] + encode(types, values)).hex()


def sqrt_price(amount0, amount1):
    require(amount0 > 0 and amount1 > 0, 'positive_pool_amounts_required')
    return math.isqrt((amount1 << 192)//amount0)


def pool_spec(a, b, amounts, decimals, values, fee, spacing):
    tokens = sorted((a, b), key=lambda value: int(value, 16))
    quantities = [amounts[t.lower()] for t in tokens]
    # Avoid floats: initial ratio and seeded balances follow fixed test valuations.
    price = sqrt_price(*quantities)
    return dict(token0=tokens[0], token1=tokens[1], fee=fee, tick_spacing=spacing,
                sqrt_price_x96=price, amounts_base=quantities,
                decimals=[decimals[t.lower()] for t in tokens],
                test_values=[values[t.lower()] for t in tokens],
                tick_lower=math.ceil(-887272/spacing)*spacing,
                tick_upper=math.floor(887272/spacing)*spacing)


class RPC:
    def __init__(self, url):
        require(url in ALLOWED_RPC and urlsplit(url).scheme == 'https', 'rpc_not_allowlisted_testnet')
        self.url = url
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        self.opener = build_opener(NoRedirect())

    def __call__(self, method, params):
        body = json.dumps(dict(jsonrpc='2.0', id=1, method=method, params=params)).encode()
        request = Request(self.url, body, {'Content-Type': 'application/json'})
        # Never log RPC params: sendRawTransaction is signed authorization data.
        try:
            with self.opener.open(request, timeout=30) as response:
                data = json.load(response)
        except Exception:
            raise RuntimeError('rpc_transport_failed:' + method) from None
        require('error' not in data and 'result' in data, 'rpc_failed:' + method)
        return data['result']


class Deployment:
    def __init__(self, rpc, bundle, journal_path, account=None, gas_budget='0.1', sender=EXPECTED_SENDER):
        self.rpc, self.bundle, self.path = rpc, bundle, Path(journal_path)
        self.account, self.sender = account, to_checksum_address(sender)
        self.budget = int(Decimal(gas_budget)*10**18)
        require(0 < self.budget <= 37*10**17, 'gas_budget_must_be_positive_and_at_most_3_7_test_MON')
        self.bundle_hash = hashlib.sha256(json.dumps(bundle, sort_keys=True).encode()).hexdigest()
        self.journal = (json.loads(self.path.read_text()) if self.path.exists() else
                        dict(schema_version=1, chain_id=CHAIN_ID, sender=self.sender,
                             bundle_sha256=self.bundle_hash, transactions={}, code_hashes={}))
        require(self.journal['chain_id'] == CHAIN_ID
                and self.journal['sender'].lower() == self.sender.lower()
                and self.journal['bundle_sha256'] == self.bundle_hash, 'journal_identity_changed')
        require(account is None or account.address.lower() == self.sender.lower(), 'unexpected_deployment_wallet')
        self.verify_chain()

    def verify_chain(self):
        require(int(self.rpc('eth_chainId', []), 16) == CHAIN_ID, 'chain_10143_required')

    def save(self):
        atomic_json(self.path, self.journal)

    def read(self, target, signature, types=(), values=(), returns=()):
        result = self.rpc('eth_call', [{'to': target, 'data': call_data(signature, types, values)}, 'latest'])
        require(result and result != '0x', 'contract_call_empty:' + signature)
        return decode(returns, bytes.fromhex(result[2:]))

    def code_hash(self, target):
        code = self.rpc('eth_getCode', [target, 'latest'])
        require(code and code != '0x', 'deployed_code_missing')
        return '0x' + keccak(bytes.fromhex(code[2:])).hex()

    def spent_and_reserved(self):
        total = 0
        for item in self.journal['transactions'].values():
            receipt = item.get('receipt')
            if receipt:
                total += int(receipt['gasUsed'], 16)*int(receipt['effectiveGasPrice'], 16)
            else:
                total += item['gas_limit']*item['gas_price_wei']
        return total

    def wait(self, item, timeout=180):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            receipt = self.rpc('eth_getTransactionReceipt', [item['hash']])
            if receipt:
                require(receipt['transactionHash'].lower() == item['hash'].lower(), 'receipt_hash_mismatch')
                block = self.rpc('eth_getBlockByNumber', [receipt['blockNumber'], False])
                require(block and block['hash'].lower() == receipt['blockHash'].lower(), 'receipt_not_canonical')
                tx = self.rpc('eth_getTransactionByHash', [item['hash']])
                require(tx and tx['from'].lower() == self.sender.lower()
                        and (tx.get('to') or ZERO).lower() == (item['intent']['to'] or ZERO).lower()
                        and tx.get('input', '0x').lower() == item['intent']['data'].lower()
                        and int(tx['value'], 16) == item['intent']['value'], 'receipt_transaction_mismatch')
                require('effectiveGasPrice' in receipt and 'gasUsed' in receipt, 'receipt_fee_missing')
                item['receipt'] = receipt
                # Failed receipts consume budget and must be checkpointed too.
                self.save()
                require(int(receipt['status'], 16) == 1, 'transaction_reverted_no_automatic_retry')
                require(self.spent_and_reserved() <= self.budget, 'gas_budget_exceeded')
                return receipt
            time.sleep(2)
        raise RuntimeError('transaction_unconfirmed_resume_same_journal')

    def transact(self, name, target=None, data='0x', value=0):
        self.verify_chain()
        intent = dict(to=target, data=data, value=value)
        existing = self.journal['transactions'].get(name)
        if existing:
            require(existing['intent'] == intent, 'checkpoint_transaction_intent_changed:' + name)
            if existing.get('receipt'):
                receipt = existing['receipt']
                block = self.rpc('eth_getBlockByNumber', [receipt['blockNumber'], False])
                require(block and block['hash'].lower() == receipt['blockHash'].lower(), 'checkpoint_reorganized')
                require(int(receipt['status'], 16) == 1, 'previous_transaction_reverted')
                return receipt
            receipt = self.rpc('eth_getTransactionReceipt', [existing['hash']])
            transaction = self.rpc('eth_getTransactionByHash', [existing['hash']])
            if not receipt and not transaction:
                nonce = int(self.rpc('eth_getTransactionCount', [self.sender, 'pending']), 16)
                require(nonce <= existing['nonce'], 'pending_transaction_nonce_conflict_manual_reconciliation')
                result = self.rpc('eth_sendRawTransaction', [existing['signed_raw']])
                require(result.lower() == existing['hash'].lower(), 'rebroadcast_hash_mismatch')
            return self.wait(existing)
        require(self.account is not None, 'broadcast_requires_explicit_account')
        pending = [n for n, item in self.journal['transactions'].items() if not item.get('receipt')]
        require(not pending, 'unfinished_transaction_reconcile_first')
        tx = dict(from_=self.sender, data=data, value=hex(value))
        tx['from'] = tx.pop('from_')
        if target:
            tx['to'] = target
        gas = (int(self.rpc('eth_estimateGas', [tx]), 16)*120+99)//100
        require(21000 <= gas <= 20_000_000, 'unexpected_transaction_gas_limit')
        price = int(self.rpc('eth_gasPrice', []), 16)
        require(price > 0, 'invalid_gas_price')
        require(self.spent_and_reserved()+gas*price <= self.budget, 'total_gas_budget_would_exceed')
        balance = int(self.rpc('eth_getBalance', [self.sender, 'pending']), 16)
        require(balance >= value+gas*price, 'test_MON_balance_insufficient')
        nonce = int(self.rpc('eth_getTransactionCount', [self.sender, 'pending']), 16)
        unsigned = dict(chainId=CHAIN_ID, nonce=nonce, gas=gas, gasPrice=price, data=data, value=value)
        if target:
            unsigned['to'] = to_checksum_address(target)
        signed = self.account.sign_transaction(unsigned)
        raw = '0x' + bytes(signed.raw_transaction).hex()
        tx_hash = '0x' + bytes(signed.hash).hex()
        item = dict(intent=intent, hash=tx_hash, nonce=nonce, signed_raw=raw, gas_limit=gas, gas_price_wei=price)
        self.journal['transactions'][name] = item
        self.save()  # write-ahead: ambiguous network result cannot trigger a new nonce.
        self.verify_chain()
        observed = self.rpc('eth_sendRawTransaction', [raw])
        require(observed.lower() == tx_hash.lower(), 'broadcast_hash_mismatch')
        result = self.wait(item)
        print(json.dumps(dict(step=name, hash=tx_hash, block=int(result['blockNumber'], 16))))
        return result

    def deploy(self, name, artifact, types=(), values=()):
        data = self.bundle['artifacts'][artifact]['bytecode']+encode(types, values).hex()
        receipt = self.transact('deploy:'+name, data=data)
        target = to_checksum_address(receipt['contractAddress'])
        observed = self.code_hash(target)
        recorded = self.journal['code_hashes'].get(name)
        require(recorded is None or recorded == observed, 'checkpoint_code_changed:' + name)
        self.journal['code_hashes'][name] = observed
        self.save()
        return target

    def approve(self, name, token, spender, quantity):
        # Allowances are exact seed amounts, not unlimited.
        return self.transact(name, token, call_data('approve(address,uint256)', ['address','uint256'], [spender, quantity]))

    def run(self, output_prefix):
        require(self.code_hash(WMON), 'official_testnet_WMON_missing')
        if isinstance(self.rpc, RPC):
            require(self.code_hash(WMON) == WMON_CODE_HASH, 'official_testnet_WMON_code_changed')
        if not self.journal['transactions']:
            balance = int(self.rpc('eth_getBalance',[self.sender,'pending']),16)
            require(balance >= self.budget+8*10**17, 'bootstrap_native_balance_must_cover_gas_budget_and_wrap')
        require(self.read(WMON, 'decimals()', returns=['uint8'])[0] == 18, 'WMON_decimals_mismatch')
        contracts = {}
        contracts['factory'] = self.deploy('factory', 'UniswapV3Factory')
        contracts['router'] = self.deploy('router', 'SwapRouter', ['address','address'], [contracts['factory'], WMON])
        contracts['quoter'] = self.deploy('quoter', 'QuoterV2', ['address','address'], [contracts['factory'], WMON])
        contracts['descriptor'] = self.deploy('descriptor', 'TestPositionDescriptor')
        contracts['position_manager'] = self.deploy('position_manager', 'NonfungiblePositionManager', ['address']*3,
                                                     [contracts['factory'], WMON, contracts['descriptor']])
        usdc = self.deploy('nUSDC', 'TestToken', ['string','string','uint8','uint256','address'],
                           ['Nerya Test USDC','nUSDC',6,1_000_000*10**6,self.sender])
        meme = self.deploy('nMEME', 'TestToken', ['string','string','uint8','uint256','address'],
                           ['Nerya Test Meme','nMEME',18,1_000_000*10**18,self.sender])
        tokens = {WMON.lower():dict(symbol='WMON',decimals=18,test_value='1'),
                  usdc.lower():dict(symbol='nUSDC',decimals=6,test_value='1'),
                  meme.lower():dict(symbol='nMEME',decimals=18,test_value='0.001')}
        feeds = {}
        for token, meta in tokens.items():
            name = meta['symbol']+'_test_feed'
            contracts[name] = self.deploy(name, 'FixedTestPriceFeed', ['int256'], [int(Decimal(meta['test_value'])*10**8)])
            feeds[token] = dict(reviewed=True, address=contracts[name], heartbeat_seconds=3600, valuation_mode='fixed_test_units')
            meta.update(reviewed=True, transfer_behavior='standard', code_hash=self.code_hash(token), testnet=True)
        for name in ('router', 'quoter', 'position_manager'):
            require(self.read(contracts[name], 'factory()', returns=['address'])[0].lower() == contracts['factory'].lower()
                    and self.read(contracts[name], 'WETH9()', returns=['address'])[0].lower() == WMON.lower(),
                    'canonical_periphery_identity_mismatch')
        # Wrap 0.8 test MON: 0.15 per pool, at least 0.5 remains for gateway swaps.
        self.transact('wrap_test_MON', WMON, call_data('deposit()'), 8*10**17)
        decimals = {token:meta['decimals'] for token,meta in tokens.items()}
        values = {token:meta['test_value'] for token,meta in tokens.items()}
        pools = {}
        for pool_id, a, b, amounts, fee, spacing in (
                ('wmon_nusdc', WMON, usdc, {WMON.lower():15*10**16, usdc.lower():150_000}, 500, 10),
                ('nmeme_wmon', meme, WMON, {WMON.lower():15*10**16, meme.lower():150*10**18}, 3000, 60)):
            spec = pool_spec(a, b, amounts, decimals, values, fee, spacing)
            manager = contracts['position_manager']
            self.transact('initialize:'+pool_id, manager, call_data('createAndInitializePoolIfNecessary(address,address,uint24,uint160)',
                          ['address','address','uint24','uint160'], [spec['token0'],spec['token1'],fee,spec['sqrt_price_x96']]))
            pool = self.read(contracts['factory'], 'getPool(address,address,uint24)', ['address','address','uint24'],
                             [spec['token0'],spec['token1'],fee], ['address'])[0]
            require(pool != ZERO, 'canonical_factory_pool_missing')
            for index in (0,1):
                self.approve('approve:'+pool_id+':'+str(index), spec['token'+str(index)], manager, spec['amounts_base'][index])
            # Journal the deadline so a confirmed mint can be replay-verified exactly.
            step = 'seed:'+pool_id
            deadlines = self.journal.setdefault('deadlines', {})
            if step not in deadlines:
                deadlines[step] = int(time.time())+1800
                self.save()
            mint_type = '(address,address,uint24,int24,int24,uint256,uint256,uint256,uint256,address,uint256)'
            params = (spec['token0'],spec['token1'],fee,spec['tick_lower'],spec['tick_upper'],
                      *spec['amounts_base'], 0, 0, self.sender, deadlines[step])
            receipt = self.transact(step, manager, call_data('mint('+mint_type+')', [mint_type], [params]))
            liquidity = self.read(pool, 'liquidity()', returns=['uint128'])[0]
            require(liquidity > 0, 'seeded_pool_liquidity_missing')
            require(self.read(pool,'factory()',returns=['address'])[0].lower() == contracts['factory'].lower(), 'pool_factory_mismatch')
            token_id = None
            transfer_topic = '0x'+keccak(text='Transfer(address,address,uint256)').hex()
            for log in receipt['logs']:
                if log['address'].lower() == manager.lower() and log['topics'][0] == transfer_topic and len(log['topics']) == 4:
                    require(int(log['topics'][1],16) == 0 and int(log['topics'][2],16) == int(self.sender,16), 'seed_NFT_owner_mismatch')
                    token_id = int(log['topics'][3],16)
            require(token_id is not None, 'seed_NFT_receipt_missing')
            pools[pool_id] = {key:spec[key] for key in ('token0','token1','fee','tick_spacing')}
            pools[pool_id].update(address=pool, reviewed=True, code_hash=self.code_hash(pool),
                                  seed_token_id=token_id, seed_liquidity=str(liquidity), testnet=True)
            for index in (0,1):
                self.transact('revoke:'+pool_id+':'+str(index), spec['token'+str(index)],
                              call_data('approve(address,uint256)', ['address','uint256'], [manager,0]))
        start = min(int(row['receipt']['blockNumber'],16) for row in self.journal['transactions'].values())
        deployment = dict(reviewed=True, chain_id=CHAIN_ID, testnet=True, official=False,
                          deployment_origin='canonical_test_deployment', protocol_variant='uniswap_v3',
                          source_revision=self.bundle['source_revision'], wrapped_native=WMON, valuation_mode='fixed_test_units',
                          contracts=contracts, code_hashes={key:self.code_hash(value) for key,value in contracts.items()},
                          pools=pools, tokens=tokens, price_feeds=feeds, start_block=start,
                          operator=self.sender, artifact_bundle_sha256=self.bundle_hash)
        prefix = Path(output_prefix)
        atomic_json(str(prefix)+'.deployment.json', deployment)
        atomic_json(str(prefix)+'.config.json', {'financial':{'defi':{'deployments':{'monad-testnet':{'uniswap_v3':deployment}}}}})
        self.journal['completed'] = True
        self.save()
        print(json.dumps(dict(completed=True, chain_id=CHAIN_ID, pools={key:value['address'] for key,value in pools.items()},
                              gas_spent_MON=str(Decimal(self.spent_and_reserved())/10**18), output_prefix=str(prefix))))
        return deployment


def load_bundle(path):
    bundle = json.loads(Path(path).read_text())
    require(bundle['source_revision'] == EXPECTED_REVISION and bundle['schema_version'] == 1, 'unreviewed_artifact_bundle')
    require(set(bundle['artifacts']) == set(PINNED_CREATION_SHA256), 'artifact_set_changed')
    for name, artifact in bundle['artifacts'].items():
        bytecode = artifact['bytecode']
        require(bytecode.startswith('0x') and len(bytes.fromhex(bytecode[2:])) > 0, 'invalid_artifact:'+name)
        require(hashlib.sha256(bytes.fromhex(bytecode[2:])).hexdigest() == PINNED_CREATION_SHA256[name],
                'pinned_creation_bytecode_changed:'+name)
    return bundle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--broadcast', action='store_true', help='Explicitly sign and broadcast to allowlisted Monad testnet only')
    parser.add_argument('--rpc', default=DEFAULT_RPC)
    parser.add_argument('--artifacts', default=str(ROOT/'build/artifacts.json'))
    parser.add_argument('--journal', required=True)
    parser.add_argument('--output-prefix', required=True)
    parser.add_argument('--gas-budget-mon', default='0.1')
    args = parser.parse_args()
    bundle = load_bundle(args.artifacts)
    rpc = RPC(args.rpc)
    journal = Path(args.journal)
    journal.parent.mkdir(parents=True, exist_ok=True)
    with open(str(journal)+'.lock','a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
        # Key path is environment-only; dry-run never reads it.
        account = None
        if args.broadcast:
            key_path = os.environ.get('MONAD_TESTNET_PRIVATE_KEY_PATH')
            require(key_path, 'MONAD_TESTNET_PRIVATE_KEY_PATH_required')
            key = Path(key_path).read_text().strip()
            account = Account.from_key(key)
            del key
        runner = Deployment(rpc, bundle, journal, account, args.gas_budget_mon)
        if args.broadcast:
            runner.run(args.output_prefix)
        else:
            balance = int(rpc('eth_getBalance',[runner.sender,'latest']),16)
            print(json.dumps(dict(broadcast=False, chain_id=CHAIN_ID, wallet=runner.sender,
                                  balance_test_MON=str(Decimal(balance)/10**18), gas_budget_MON=args.gas_budget_mon,
                                  wrapped_native=WMON, wrapped_native_code_hash=runner.code_hash(WMON),
                                  source_revision=bundle['source_revision'], bundle_sha256=runner.bundle_hash,
                                  planned_native_wrap_MON='0.8', pools=['wmon_nusdc','nmeme_wmon'])))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Never print a traceback/local variables from secret parsing/signing.
        print(json.dumps({'ok':False,'error':str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__}))
        raise SystemExit(1)
