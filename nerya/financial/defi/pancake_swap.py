"""Reviewed Monad PancakeSwap V3 single-hop ERC20 swaps. No implicit approvals.

ABI: pancake-v3-contracts ISwapRouter, IQuoterV2 and IPancakeV3PoolEvents.
Deployment ``tokens`` must attest reviewed code_hash, decimals and
transfer_behavior='standard'; taxed/rebasing/unknown tokens fail closed.
Input valuation uses the same reviewed Chainlink-style price_feeds as LP.
Native wrapping, multihop and exact-output swaps are deliberately unsupported.
Monad testnet requires explicit chain_id=10143, testnet=True, official=False,
deployment_origin='canonical_test_deployment', protocol_variant ('pancakeswap_v3'
or 'uniswap_v3'), source_revision and wrapped_native in the reviewed deployment.
Its on-chain price feeds are test valuations, not real-world token prices.
"""
import time
from decimal import Decimal

from eth_utils import keccak
from eth_abi.exceptions import DecodingError

from ..adapters import units
from ..contracts import FinancialError, amount, digest, security_revision
from .calls import address, calldata, matching_event, require, topic_address, transaction
from .transport import ProtocolFunds

FACTORY = '0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865'
ROUTER = '0x1b81D678ffb9C0263b24A97847620C99d213eB14'
QUOTER = '0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997'
WMON = '0x3bd359C1119dA7Da1D913D1C4D2B7c461115433A'
SWAP_TUPLE = '(address,address,uint24,address,uint256,uint256,uint256,uint160)'
QUOTE_TUPLE = '(address,address,uint256,uint24,uint160)'
SWAP_EVENT = 'Swap(address,address,int256,int256,uint160,uint128,int24,uint128,uint128)'
UNI_SWAP_EVENT = 'Swap(address,address,int256,int256,uint160,uint128,int24)'
SWAP_SCHEMA = {
    'type': 'object',
    'properties': {
        'pool_id': {'type': 'string', 'minLength': 1},
        'token_out': {'type': 'string', 'pattern': r'^0x[0-9a-fA-F]{40}$'},
    },
    'required': ['pool_id', 'token_out'],
    'additionalProperties': False,
}


class V3SwapTransport(ProtocolFunds):
    def __init__(self, config, request, **kwargs):
        supported = (request.get('chain') == 'monad' and request.get('protocol') == 'pancakeswap_v3'
                     or request.get('chain') == 'monad-testnet'
                     and request.get('protocol') in {'pancakeswap_v3', 'uniswap_v3'})
        if (request.get('kind') != 'swap' or not supported
                or request.get('chain') not in {'monad', 'monad-testnet'} or not request.get('wallet_id')):
            raise FinancialError('unsupported_monad_pancake_swap', 422)
        super().__init__(config, request, **kwargs)
        contracts = self.deployment['contracts']
        self.variant = 'pancakeswap_v3'
        self.wrapped_native = address(WMON)
        if self.chain == 'monad':
            require(self.chain_spec.chain_id == 143 and not self.chain_spec.testnet, 'monad_mainnet_required')
            for key, expected in (('factory', FACTORY), ('router', ROUTER), ('quoter', QUOTER)):
                require(str(contracts.get(key, '')).lower() == expected.lower(), 'pancake_official_deployment_required')
        else:
            require(self.chain_spec.chain_id == 10143 and self.chain_spec.testnet, 'monad_testnet_required')
            require(self.deployment.get('chain_id') == 10143 and self.deployment.get('testnet') is True
                    and self.deployment.get('official') is False
                    and self.deployment.get('deployment_origin') == 'canonical_test_deployment',
                    'swap_testnet_deployment_identity_required')
            self.variant = self.deployment.get('protocol_variant')
            require(self.variant == request['protocol']
                    and isinstance(self.deployment.get('source_revision'), str)
                    and bool(self.deployment['source_revision'].strip()), 'swap_canonical_source_evidence_required')
            self.wrapped_native = address(self.deployment.get('wrapped_native'))
        self.factory, self.router, self.quoter = (address(contracts.get(key)) for key in ('factory', 'router', 'quoter'))
        if self.chain_spec.testnet:
            for target in (self.router, self.quoter):
                require(self.call(target, 'factory()', [], [], ['address'])[0].lower() == self.factory.lower()
                        and self.call(target, 'WETH9()', [], [], ['address'])[0].lower() == self.wrapped_native.lower(),
                        'swap_testnet_periphery_identity_mismatch')
        self.swap_event = SWAP_EVENT if self.variant == 'pancakeswap_v3' else UNI_SWAP_EVENT
        self.wallet_id = request['wallet_id']

    def _identity(self, request):
        require(request.get('kind') == 'swap' and request.get('protocol') == self.protocol
                and request.get('chain') == self.chain and request.get('wallet_id') == self.wallet_id, 'swap_request_identity_mismatch')
        p = request.get('parameters')
        require(isinstance(p, dict) and set(p) == {'pool_id', 'token_out'}
                and isinstance(p['pool_id'], str) and bool(p['pool_id']), 'invalid_swap_parameters')
        slippage = request.get('slippage_bps', 50)
        require(type(slippage) is int and 0 <= slippage <= 1000, 'invalid_slippage')
        return {'wallet_id': request['wallet_id'], 'chain': self.chain, 'protocol': self.protocol,
                'kind': 'swap', 'token_in': address(request.get('asset')),
                'token_out': address(p['token_out']), 'pool_id': p['pool_id'],
                'amount': format(amount(request.get('amount')), 'f'), 'slippage_bps': slippage}

    def _code(self, target, expected, block, reason):
        code = self.connector._rpc('eth_getCode', [target, block])
        require(bool(expected) and bool(code) and code != '0x'
                and '0x' + keccak(bytes.fromhex(code[2:])).hex() == str(expected).lower(), reason)

    def _block(self):
        self.chain_spec.verify(self.connector)
        row = self.connector._rpc('eth_getBlockByNumber', ['latest', False])
        require(isinstance(row, dict) and bool(row.get('hash')), 'swap_block_evidence_missing')
        require(0 <= time.time() - int(row['timestamp'], 16) <= 30, 'swap_chain_state_stale')
        return row

    def _pool(self, identity, block):
        raw = (self.deployment.get('pools') or {}).get(identity['pool_id'])
        require(bool(raw) and raw.get('reviewed') is True, 'swap_pool_not_reviewed')
        a, b = address(raw['token0']), address(raw['token1'])
        fee = raw['fee']
        require(int(a, 16) < int(b, 16) and type(fee) is int and 0 < fee < 1_000_000, 'invalid_swap_pool')
        require(identity['token_in'] != identity['token_out']
                and {identity['token_in'], identity['token_out']} == {a, b}, 'swap_pool_token_mismatch')
        pool = address(raw['address'])
        observed = self.call(self.factory, 'getPool(address,address,uint24)',
                             ['address', 'address', 'uint24'], [a, b, fee], ['address'], block)[0]
        require(observed.lower() == pool.lower(), 'swap_pool_identity_changed')
        self._code(pool, raw.get('code_hash'), block, 'swap_pool_code_changed')
        for signature, expected, typ in (('factory()', self.factory.lower(), 'address'),
                                          ('token0()', a.lower(), 'address'), ('token1()', b.lower(), 'address'),
                                          ('fee()', fee, 'uint24')):
            actual = self.call(pool, signature, [], [], [typ], block)[0]
            require((actual.lower() if typ == 'address' else actual) == expected, 'swap_pool_identity_changed')
        sqrt, _, _, _, _, _, unlocked = self.call(pool, 'slot0()', [], [],
            ['uint160', 'int24', 'uint16', 'uint16', 'uint16',
             'uint32' if self.variant == 'pancakeswap_v3' else 'uint8', 'bool'], block)
        require(sqrt > 0 and unlocked, 'swap_pool_uninitialized_or_locked')
        require(self.call(pool, 'liquidity()', [], [], ['uint128'], block)[0] > 0, 'swap_pool_empty')
        decimals = []
        for token in (a, b):
            attestation = (self.deployment.get('tokens') or {}).get(token.lower())
            require(bool(attestation) and attestation.get('reviewed') is True
                    and attestation.get('transfer_behavior') == 'standard', 'swap_nonstandard_token_unsupported')
            self._code(token, attestation.get('code_hash'), block, 'swap_token_code_changed')
            value = self.call(token, 'decimals()', [], [], ['uint8'], block)[0]
            require(type(attestation.get('decimals')) is int and value == attestation['decimals'], 'swap_token_decimals_changed')
            decimals.append(value)
        return {'pool_id': identity['pool_id'], 'address': pool, 'token0': a, 'token1': b, 'fee': fee, 'decimals': decimals}

    def _price(self, token, block):
        feed = (self.deployment.get('price_feeds') or {}).get(token.lower())
        require(bool(feed) and feed.get('reviewed') is True, 'swap_input_price_feed_not_reviewed')
        target = address(feed['address'])
        require(target.lower() in {value.lower() for value in self.deployment['contracts'].values()}, 'swap_price_feed_code_evidence_missing')
        heartbeat = feed.get('heartbeat_seconds')
        require(type(heartbeat) is int and 0 < heartbeat <= 3600, 'swap_price_feed_heartbeat_required')
        rid, answer, _, updated, answered = self.call(target, 'latestRoundData()', [], [],
            ['uint80', 'int256', 'uint256', 'uint256', 'uint80'], block)
        require(answer > 0 and answered >= rid and 0 <= time.time() - updated <= heartbeat, 'swap_input_price_stale')
        decimals = self.call(target, 'decimals()', [], [], ['uint8'], block)[0]
        return Decimal(answer) / 10**decimals

    def _quote_output(self, identity, pool, quantity, block):
        output, sqrt, ticks, gas = self.call(self.quoter, 'quoteExactInputSingle(' + QUOTE_TUPLE + ')',
            [QUOTE_TUPLE], [(identity['token_in'], identity['token_out'], quantity, pool['fee'], 0)],
            ['uint256', 'uint160', 'uint32', 'uint256'], block)
        require(output > 0 and sqrt > 0 and gas > 0, 'swap_quote_output_unavailable')
        return {'amount_out_base': str(output), 'sqrt_price_after_x96': str(sqrt),
                'initialized_ticks_crossed': ticks, 'quoter_gas_estimate': gas}

    def _funds(self, token, quantity, block, *, require_allowance):
        balance = self.call(token, 'balanceOf(address)', ['address'], [self.sender], ['uint256'], block)[0]
        require(balance >= quantity, 'swap_input_balance_insufficient')
        allowance = self.call(token, 'allowance(address,address)', ['address', 'address'],
                              [self.sender, self.router], ['uint256'], block)[0]
        require(allowance < 2**256 - 1, 'finite_protocol_allowance_required')
        if require_allowance:
            require(allowance >= quantity, 'finite_protocol_allowance_required')
        return allowance

    def _transaction(self, identity, pool, quantity, deadline, minimum):
        return transaction(self.sender, self.router, calldata('exactInputSingle(' + SWAP_TUPLE + ')',
            [SWAP_TUPLE], [(identity['token_in'], identity['token_out'], pool['fee'],
                           self.sender, deadline, quantity, minimum, 0)]))

    def transaction_quote(self, request, tx, *, risk_usd, spend_assets, prices, prerequisites=()):
        # MON gas uses the reviewed WMON feed, never an unrelated ticker API.
        cap = self.config.get('financial.defi.max_gas_limit')
        require(type(cap) is int and 21000 <= cap <= 10_000_000, 'protocol_gas_limit_required')
        gas_price = int(self.connector._rpc('eth_gasPrice', []), 16)
        native_price = self._price(self.wrapped_native, 'latest')
        balances = {'NATIVE': str(Decimal(int(self.connector._rpc('eth_getBalance', [self.sender, 'latest']), 16)) / 10**18)}
        for token in spend_assets:
            decimals = self.call(token, 'decimals()', [], [], ['uint8'])[0]
            balance = self.call(token, 'balanceOf(address)', ['address'], [self.sender], ['uint256'])[0]
            balances[token] = str(Decimal(balance) / 10**decimals)
        # Generate deadline-bearing calldata only after read-only valuation and
        # balances, so slow providers cannot expire it before gas estimation.
        if callable(tx):tx = tx()
        gas = cap if prerequisites else (int(self.connector._rpc('eth_estimateGas', [tx]), 16) * 120 + 99) // 100
        require(gas <= cap, 'protocol_gas_limit_exceeded')
        gas_units = Decimal(gas * gas_price) / 10**18
        return {'risk_usd': str(risk_usd),
                'spend_usd': str(sum((amount(value) * prices[token] for token, value in spend_assets.items()), Decimal(0))),
                'fee_usd': str(gas_units * native_price),
                'asset_amounts': {**spend_assets, 'NATIVE': str(gas_units)}, 'available_asset_amounts': balances,
                'transaction': tx, 'gas_limit': gas, 'gas_price_wei': str(gas_price),
                'native_price_usd': str(native_price), 'chain_id': self.chain_spec.chain_id, 'sender': self.sender,
                'expires_at': time.time() + 30, 'deployment_revision': security_revision(self.deployment),
                'prerequisites': list(prerequisites),
                'steps': [{'kind': 'protocol_call', 'router': self.router, 'chain': self.chain}]}

    def quote(self, request):
        identity = self._identity(request)
        block = self._block()
        pool = self._pool(identity, block['number'])
        index = int(identity['token_in'] == pool['token1'])
        quantity = units(identity['amount'], pool['decimals'][index])
        require(quantity > 0, 'swap_input_amount_required')
        evidence = self._quote_output(identity, pool, quantity, block['number'])
        minimum = int(evidence['amount_out_base']) * (10000 - identity['slippage_bps']) // 10000
        require(minimum > 0, 'swap_nonzero_minimum_output_required')
        window = self.config.get('financial.defi.quote_window_seconds', 30)
        maximum_window = 300 if self.chain_spec.testnet else 120
        require(type(window) is int and 30 <= window <= maximum_window, 'invalid_protocol_quote_window')
        allowance = self._funds(identity['token_in'], quantity, block['number'], require_allowance=False)
        prerequisites = [] if allowance >= quantity else [{'kind': 'contract_approval',
            'wallet_id': request['wallet_id'], 'chain': self.chain, 'asset': identity['token_in'],
            'amount': identity['amount'], 'spender': self.router}]
        price = self._price(identity['token_in'], block['number'])
        spend = {identity['token_in']: identity['amount']}
        deadlines = []
        def build_transaction():
            deadlines.append(int(time.time()) + window)
            return self._transaction(identity, pool, quantity, deadlines[-1], minimum)
        result = self.transaction_quote(request, build_transaction, risk_usd=amount(identity['amount']) * price,
            spend_assets=spend, prices={identity['token_in']: price}, prerequisites=prerequisites)
        deadline = deadlines[0]
        require(amount(result['available_asset_amounts']['NATIVE'], zero=True) >= amount(result['asset_amounts']['NATIVE']), 'swap_gas_balance_insufficient')
        return {**result, 'expires_at': min(time.time() + window, deadline), 'request_identity': identity,
                'network': {'chain_id': self.chain_spec.chain_id, 'testnet': self.chain_spec.testnet,
                            'protocol_variant': self.variant,
                            'deployment_origin': 'canonical_test_deployment' if self.chain_spec.testnet else 'official',
                            'valuation_mode': 'test_units' if self.chain_spec.testnet else 'reviewed_price_feed'},
                'exposure_asset_amounts': {identity['token_out']: str(Decimal(int(evidence['amount_out_base'])) / 10**pool['decimals'][1-index])},
                'request_digest': digest(identity), 'pool': pool, 'amount_in_base': str(quantity),
                'minimum_out_base': str(minimum), 'deadline': deadline, 'input_price_usd': str(price),
                'quote_block': {'number': block['number'], 'hash': block['hash']}, **evidence}

    def _frozen(self, request, quote):
        identity = self._identity(request)
        require(identity == quote['request_identity'] and digest(identity) == quote['request_digest'], 'swap_request_identity_mismatch')
        pool = quote['pool']
        require(identity['pool_id'] == pool['pool_id'] and {identity['token_in'], identity['token_out']}
                == {pool['token0'], pool['token1']}, 'swap_plan_mismatch')
        quantity = units(identity['amount'], pool['decimals'][int(identity['token_in'] == pool['token1'])])
        minimum = int(quote['amount_out_base']) * (10000 - identity['slippage_bps']) // 10000
        require(quantity > 0 and quantity == int(quote['amount_in_base']) and minimum > 0
                and minimum == int(quote['minimum_out_base']), 'swap_plan_mismatch')
        require(quote['transaction'] == self._transaction(identity, pool, quantity, quote['deadline'], minimum)
                and quote['sender'] == self.sender and quote['chain_id'] == self.chain_spec.chain_id, 'swap_plan_mismatch')
        require(quote['network'] == {'chain_id': self.chain_spec.chain_id, 'testnet': self.chain_spec.testnet,
                'protocol_variant': self.variant,
                'deployment_origin': 'canonical_test_deployment' if self.chain_spec.testnet else 'official',
                'valuation_mode': 'test_units' if self.chain_spec.testnet else 'reviewed_price_feed'}, 'swap_network_plan_mismatch')
        price = amount(quote['input_price_usd'])
        value = amount(identity['amount']) * price
        require(amount(quote['risk_usd']) == value and amount(quote['spend_usd']) == value
                and amount(quote['asset_amounts'][identity['token_in']]) == amount(identity['amount']), 'swap_accounting_plan_mismatch')
        expected_exposure = {identity['token_out']: str(Decimal(int(quote['amount_out_base']))
            / 10**pool['decimals'][int(identity['token_out'] == pool['token1'])])}
        require(quote['exposure_asset_amounts'] == expected_exposure, 'swap_accounting_plan_mismatch')
        return identity, pool, quantity, minimum

    def validate(self, request, quote):
        identity, pool, quantity, minimum = self._frozen(request, quote)
        require(self.config.get('financial.wallet_permissions', {}).get(self.wallet_id, {}).get('swap') is True,
                'wallet_funds_permission_disabled')
        require(min(quote['deadline'], quote['expires_at']) > time.time(), 'financial_quote_expired')
        # Check allowance before estimation, which can revert when approval is absent.
        self._funds(identity['token_in'], quantity, 'latest', require_allowance=True)
        canonical = self.connector._rpc('eth_getBlockByNumber', [quote['quote_block']['number'], False])
        require(bool(canonical) and canonical.get('hash') == quote['quote_block']['hash'], 'swap_quote_block_reorganized')
        block = self._block()
        require(self._pool(identity, block['number']) == pool, 'swap_pool_identity_changed')
        self._funds(identity['token_in'], quantity, block['number'], require_allowance=True)
        fresh = self._quote_output(identity, pool, quantity, block['number'])
        require(int(fresh['amount_out_base']) >= minimum, 'swap_requote_slippage_exceeded')
        require(self._price(identity['token_in'], block['number']) <= amount(quote['input_price_usd']), 'swap_input_valuation_changed')
        native = int(self.connector._rpc('eth_getBalance', [self.sender, block['number']]), 16)
        require(native >= quote['gas_limit'] * int(quote['gas_price_wei']), 'swap_gas_balance_insufficient')
        super().validate(request, quote)

    def status(self, request, quote, submission):
        result, receipt = self.verified_receipt(quote, submission)
        if receipt is None:
            return result
        try:
            identity, pool, quantity, minimum = self._frozen(request, quote)
            require(receipt.get('transactionHash', '').lower() == submission['transaction_hash'].lower(), 'swap_receipt_transaction_mismatch')
            require(self._pool(identity, receipt['blockNumber']) == pool, 'swap_pool_identity_changed')
            logs = receipt.get('logs') or []
            for log in logs:
                require(not log.get('removed') and log.get('transactionHash', '').lower() == submission['transaction_hash'].lower()
                        and log.get('blockHash') == receipt['blockHash'], 'swap_receipt_log_identity_mismatch')
            event_types = ['int256', 'int256', 'uint160', 'uint128', 'int24']
            if self.variant == 'pancakeswap_v3':
                event_types += ['uint128', 'uint128']
            swap_log, values = matching_event(logs, pool['address'], self.swap_event,
                [topic_address(self.router), topic_address(self.sender)],
                event_types)
            incoming, outgoing = values[:2] if identity['token_in'] == pool['token0'] else values[:2][::-1]
            require(incoming == quantity and outgoing < 0 and -outgoing >= minimum, 'swap_fill_bounds_mismatch')
            transfer_signature = 'Transfer(address,address,uint256)'
            proof = []
            for token, source, destination, expected in (
                (identity['token_in'], self.sender, pool['address'], incoming),
                (identity['token_out'], pool['address'], self.sender, -outgoing),
            ):
                log, amounts = matching_event(logs, token, transfer_signature,
                    [topic_address(source), topic_address(destination)], ['uint256'])
                require(amounts[0] == expected, 'swap_transfer_tax_or_accounting_mismatch')
                # Reject extra wallet-affecting transfers, including tax/refund legs.
                related = [row for row in logs if row.get('address', '').lower() == token.lower()
                    and len(row.get('topics') or []) == 3
                    and row['topics'][0].lower() == '0x' + keccak(text=transfer_signature).hex()
                    and topic_address(self.sender).lower() in [topic.lower() for topic in row['topics'][1:]]]
                require(len(related) == 1, 'swap_extra_wallet_transfer_unsupported')
                proof.append(int(log['logIndex'], 16))
            require(len(set([*proof, int(swap_log['logIndex'], 16)])) == 3, 'swap_receipt_log_index_ambiguous')
            decimals = pool['decimals']
            input_index = int(identity['token_in'] == pool['token1'])
            debit = Decimal(incoming) / 10**decimals[input_index]
            credit = Decimal(-outgoing) / 10**decimals[1 - input_index]
            return {'state': 'confirmed', **result, 'network': quote['network'], 'amount_in_base': str(incoming),
                    'amount_out_base': str(-outgoing), 'amount_in': str(debit), 'amount_out': str(credit),
                    'settled_notional_usd': str(debit * amount(quote['input_price_usd'])),
                    'cashflows': [{'asset': identity['token_in'], 'debit_base': str(incoming), 'credit_base': '0'},
                                  {'asset': identity['token_out'], 'debit_base': '0', 'credit_base': str(-outgoing)}],
                    'protocol_effect': {'pool': pool['address'], 'swap_log_index': int(swap_log['logIndex'], 16),
                                        'transfer_log_indices': proof}}
        except FinancialError as exc:
            return {'state': 'needs_recovery', **result, 'reason': exc.code}
        except (KeyError, ValueError, TypeError, OverflowError, DecodingError):
            return {'state': 'needs_recovery', **result, 'reason': 'swap_receipt_evidence_malformed'}


# Existing integration name; both names share the exact same network fences.
PancakeSwapTransport = V3SwapTransport
