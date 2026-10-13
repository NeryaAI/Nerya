"""Monad Perpl REST transport; all writes require a reserved FinancialGateway action."""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import re
import sqlite3
import time
from decimal import Decimal, ROUND_DOWN, ROUND_CEILING, localcontext
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from nerya.financial.contracts import FinancialError, amount, digest
from nerya.security.secrets import SecretVault
from nerya.trading.components import TradingComponentSpec

API = 'https://app.perpl.xyz/api'
RPC = 'https://rpc.monad.xyz'
NETWORKS = {143: (API, RPC), 10143: ('https://testnet.perpl.xyz/api', 'https://testnet-rpc.monad.xyz')}
MAX_UINT64 = 2**64 - 1


def integer(value, *, positive=False):
    if type(value) is not int or value < int(positive) or value > MAX_UINT64:
        raise FinancialError('perpl_invalid_integer', 400)
    return value


def scaled(value, decimals):
    integer(decimals)
    if decimals > 18:
        raise FinancialError('perpl_invalid_decimals', 422)
    with localcontext() as ctx:
        ctx.prec = 100
        result = amount(value) * Decimal(10)**decimals
        if result != result.to_integral_value() or result > MAX_UINT64:
            raise FinancialError('perpl_off_tick_or_lot', 400)
        return integer(int(result), positive=True)


def unscaled(value, decimals):
    return Decimal(integer(value)) / Decimal(10)**decimals


def transaction_hash(value):
    """Perpl wire txid may be bare hex; Ethereum JSON-RPC requires 0x."""
    if not isinstance(value,str):
        raise FinancialError('perpl_invalid_transaction_hash',422)
    raw=value.removeprefix('0x')
    if not re.fullmatch(r'[a-fA-F0-9]{64}',raw):
        raise FinancialError('perpl_invalid_transaction_hash',422)
    return '0x'+raw.lower()


def b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


def signed_headers(token, seed, method, target, body=b'', *, timestamp=None, nonce=None, chain_id=143):
    """Canonical target excludes /api, exactly as the official signing example."""
    if chain_id not in NETWORKS:
        raise FinancialError('perpl_network_unsupported', 400)
    timestamp = str(timestamp if timestamp is not None else int(time.time()*1000))
    nonce = nonce or b64(secrets.token_bytes(16))
    canonical = '\n'.join((str(chain_id), method, target, timestamp, nonce, hashlib.sha256(body).hexdigest())).encode()
    key = Ed25519PrivateKey.from_private_bytes(seed)
    return {'X-API-Key': token, 'X-API-Timestamp': timestamp, 'X-API-Nonce': nonce,
            'X-API-Signature': b64(key.sign(canonical))}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HTTP:
    def request(self, method, url, *, headers=None, body=b''):
        req = Request(url, data=body if method == 'POST' else None, headers=headers or {}, method=method)
        try:
            with build_opener(NoRedirect()).open(req, timeout=15) as response:
                return response.status, json.loads(response.read())
        except HTTPError as exc:
            # Never echo venue error bodies: they can contain signed request material.
            return exc.code, {}
        except Exception:
            raise FinancialError('perpl_transport_unavailable', 503) from None


class PerplTransport:
    def __init__(self, config, request, *, http=None, vault=None):
        self.config = config
        self.http = http or HTTP()
        self.vault = vault
        self.aid = request.get('account_id')
        self.settings = dict(config.get('plugins.perpl.accounts', {}).get(self.aid, {}))
        required = {'chain_id', 'wallet_address', 'exchange_account_id', 'instance_id',
                    'api_key_ref', 'private_key_ref', 'public_key_hex', 'approved_scopes'}
        if required - self.settings.keys() or self.settings['chain_id'] not in NETWORKS:
            raise FinancialError('perpl_account_unconfigured', 422)
        self.chain_id = self.settings['chain_id']
        self.api, self.rpc = NETWORKS[self.chain_id]
        if self.settings.get('api_url', self.api) != self.api or self.settings.get('rpc_url', self.rpc) != self.rpc:
            raise FinancialError('perpl_configured_network_mismatch', 403)
        from nerya.financial.adapters import validate_evm_address
        validate_evm_address(self.settings['wallet_address'])
        integer(self.settings['exchange_account_id'], positive=True)
        integer(self.settings['instance_id'], positive=True)
        for field in ('api_key_ref', 'private_key_ref'):
            if not str(self.settings[field]).startswith('vault://'):
                raise FinancialError('perpl_vault_reference_required', 403)
        TRADING_COMPONENTS[0].validate_request(request)
        if request.get('chain') != ('monad' if self.chain_id == 143 else 'monad-testnet') or request.get('account_id') != self.aid or request.get('kind') != 'trade':
            raise FinancialError('perpl_resource_mismatch', 403)

    def _vault(self):
        if self.vault is None:
            self.vault = SecretVault.open(self.config.paths.vault_enc, getattr(self.config, 'vault_passphrase', None))
        return self.vault

    def _request(self, method, target, payload=None, *, private=False, trade=False):
        body = json.dumps(payload, separators=(',', ':'), ensure_ascii=False).encode() if payload is not None else b''
        headers = {'Content-Type': 'application/json'} if body else {}
        if private:
            scope = 'perpl:trade' if trade else 'perpl:read'
            if scope not in self.settings['approved_scopes']:
                raise FinancialError('perpl_operator_scope_denied', 403)
            vault = self._vault()
            try:
                token = vault.resolve(self.settings['api_key_ref'][8:], required_scope=scope)
                secret = vault.resolve(self.settings['private_key_ref'][8:], required_scope=scope)
                seed = bytes.fromhex(secret.removeprefix('0x'))
                key = Ed25519PrivateKey.from_private_bytes(seed)
                public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
                if public != self.settings['public_key_hex'].removeprefix('0x').lower():
                    raise ValueError()
                headers.update(signed_headers(token, seed, method, target, body, chain_id=self.chain_id))
            except Exception:
                raise FinancialError('perpl_signer_unavailable_or_mismatched', 403) from None
            finally:
                token = secret = seed = key = None
        status, doc = self.http.request(method, self.api + target, headers=headers, body=body)
        if status != 200:
            raise FinancialError('perpl_http_' + str(status), 503 if status >= 500 else status)
        if not isinstance(doc, dict):
            raise FinancialError('perpl_invalid_response', 502)
        return doc

    def _context(self, request):
        context = self._request('GET', '/v1/pub/context')
        rpc_status, rpc = self.http.request('POST', self.rpc, headers={'Content-Type': 'application/json'},
                                  body=b'{"jsonrpc":"2.0","id":1,"method":"eth_chainId","params":[]}')
        if rpc_status != 200 or context.get('chain', {}).get('chain_id') != self.chain_id or rpc.get('result') != hex(self.chain_id):
            raise FinancialError('perpl_chain_identity_mismatch', 403)
        symbol = str(request.get('market', '')).removeprefix('PERPL:').split('/')[0]
        if request.get('market') != 'PERPL:' + symbol + '/AUSD':
            raise FinancialError('perpl_invalid_market', 400)
        market = next((m for m in context.get('markets', []) if (m.get('name', '').removesuffix(' Perp') == symbol or m.get('size_units') == symbol)), None)
        if not market or not market.get('config', {}).get('is_open'):
            raise FinancialError('perpl_market_unavailable', 422)
        if market['instance_id'] != self.settings['instance_id']:
            raise FinancialError('perpl_instance_mismatch', 403)
        self._fresh(market['state']['at'])
        return context, market

    @staticmethod
    def _fresh(at):
        if not -5 <= time.time() - integer(at.get('t'))/1000 <= 30:
            raise FinancialError('perpl_stale_evidence', 422)

    def _account(self, *, recovery=False):
        wallet = self._request('GET', '/v1/trading/wallet', private=True)
        if wallet.get('addr', '').lower() != self.settings['wallet_address'].lower():
            raise FinancialError('perpl_wallet_identity_mismatch', 403)
        self._fresh(wallet.get('at', {}))
        account = next((a for a in wallet.get('as', []) if a.get('id') == self.settings['exchange_account_id']), None)
        if not account or account.get('in') != self.settings['instance_id']:
            raise FinancialError('perpl_account_identity_mismatch', 403)
        if not recovery and (account.get('fr') or account.get('fw') is not True):
            raise FinancialError('perpl_account_frozen_or_forwarding_disabled', 403)
        return wallet, account

    def _positions(self):
        doc = self._request('GET', '/v1/trading/positions', private=True)
        self._fresh(doc.get('at', {}))
        return [p for p in doc.get('d', []) if p.get('acc') == self.settings['exchange_account_id'] and p.get('st') == 1]

    def _binding(self):
        vault = self._vault()
        return digest({'settings': self.settings, 'secrets': [vault.meta(self.settings[f][8:]).as_public()
                      for f in ('api_key_ref', 'private_key_ref')]})

    def account_state(self, request):
        from nerya.trading.accounts import get_account_profile
        if not get_account_profile(self.config.paths, self.aid).permissions.read_balances:
            raise FinancialError('perpl_account_read_not_permitted', 403)
        context, market = self._context(request)
        wallet, account = self._account()
        positions = self._positions()
        free = max(Decimal(0), amount(account['b'], zero=True)-amount(account['lb'], zero=True))/10**6
        return {'health': 'ok', 'source': self.api, 'as_of': wallet['at']['t']/1000, 'chain_id': self.chain_id,
                'wallet_address': wallet['addr'], 'exchange_account_id': account['id'], 'account': account,
                'free_by_asset': {'AUSD': str(free)}, 'positions': positions, 'market': market,
                'chain': context['chain'], 'scope': 'authenticated observation; no order submitted'}

    def _order(self, request, market, positions):
        params = request.get('parameters', {})
        plan = request.get('plan') or {}
        if plan.get('account_id') != self.aid or plan.get('market') != request['market'] or not plan.get('strategy_id'):
            raise FinancialError('perpl_plan_resource_mismatch', 403)
        config = market['config']
        base = {'mkt': market['id'], 'acc': self.settings['exchange_account_id'], 'lb': 0}
        if params.get('operation', 'order') == 'cancel':
            if plan.get('action') != 'cancel_executor':
                raise FinancialError('perpl_cancel_plan_required', 400)
            return {**base, 'oid': integer(params.get('order_id'), positive=True), 't': 5, 's': 0, 'fl': 0, 'lv': 0}
        if plan.get('action') not in {'open_position', 'close_position', 'reduce_position'} or plan.get('side') not in {'long', 'short'}:
            raise FinancialError('perpl_unsupported_plan', 400)
        if plan.get('protection'):
            raise FinancialError('perpl_protection_requires_separate_adapter', 422)
        reduce = plan['action'] != 'open_position'
        if params.get('reduce_only', False) != reduce:
            raise FinancialError('perpl_reduce_only_mismatch', 400)
        entry = plan.get('entry') or {}
        kind = entry.get('order_type', 'market')
        if kind not in {'market', 'limit'}:
            raise FinancialError('perpl_unsupported_order_type', 422)
        decimals = config['price_decimals']
        mark = unscaled(market['state']['mrk'], decimals)
        sizing = plan.get('sizing', {})
        method = sizing.get('method')
        if method == 'fixed_base':
            size = scaled(sizing.get('fixed_base'), config['size_decimals'])
        elif method == 'fixed_usd':
            with localcontext() as ctx:
                ctx.prec = 100
                # Bound notional at the worse book side plus allowed slippage. Round down only USD sizing.
                worst = unscaled(max(market['state']['mrk'], market['state']['ask']), decimals)
                if kind == 'limit':
                    worst = max(worst, amount(entry.get('limit_price')))
                worst *= 1 + Decimal(params.get('slippage_bps', 50))/10000
                size = integer(int((amount(sizing.get('fixed_usd')) / worst * Decimal(10)**config['size_decimals']).to_integral_value(rounding=ROUND_DOWN)), positive=True)
        else:
            raise FinancialError('perpl_fixed_sizing_required', 422)
        leverage = amount((plan.get('meta') or {}).get('leverage', 1))
        lv = scaled(leverage, 2)
        # Perpl margin is inverse leverage in hundredths: 1000 => 10x.
        if leverage < 1 or leverage > Decimal(10000)/integer(config['initial_margin'], positive=True):
            raise FinancialError('perpl_leverage_out_of_range', 400)
        ms = integer(params.get('slippage_bps', 50))
        if not 1 <= ms <= min(1000, market['order_max_market_slippage_bps']):
            raise FinancialError('perpl_slippage_out_of_range', 400)
        mnp = integer(params.get('max_negative_pnl_bps', 0))
        if mnp > min(100, integer(market.get('order_max_neg_pnl_collat_bps', 0))):
            raise FinancialError('perpl_negative_pnl_budget_out_of_range', 400)
        order_type = (3 if plan['side'] == 'long' else 4) if reduce else (1 if plan['side'] == 'long' else 2)
        if reduce:
            position = next((p for p in positions if p.get('mkt') == market['id'] and p.get('sd') == (1 if plan['side']=='long' else 2)), None)
            if not position or size > integer(position['s']) or (params.get('position_id') is not None and params['position_id'] != position.get('pid')):
                raise FinancialError('perpl_close_exceeds_or_mismatches_position', 403)
        flags = {'gtc': 0, 'post_only': 1, 'fok': 2, 'ioc': 4}
        tif = entry.get('time_in_force', 'ioc' if kind == 'market' else 'gtc')
        if tif not in flags or (kind == 'market' and tif not in {'ioc', 'fok'}):
            raise FinancialError('perpl_invalid_time_in_force', 400)
        price = 0 if kind == 'market' else scaled(entry.get('limit_price'), decimals)
        notional = unscaled(size, config['size_decimals']) * (unscaled(price, decimals) if price else mark)
        minimum = amount(config['min_settle_amount' if kind=='market' else 'min_posting_amount'], zero=True)/10**6
        if notional < minimum:
            raise FinancialError('perpl_order_under_minimum', 400)
        return {**base, 't': order_type, 'p': price, 's': size, 'ms': ms, 'mnp': mnp, 'fl': flags[tif], 'lv': lv}

    def _producer_source(self, request):
        from nerya.financial.contracts import context_from_config, FinancialContext
        from nerya.financial.strategy_runtime import validate_context
        context = None
        if self.config.get('runtime.strategy_financial_run_id'):
            context = context_from_config(self.config)
            if context.strategy_run_id != self.config.get('runtime.strategy_financial_run_id'):
                raise FinancialError('perpl_strategy_run_binding_mismatch',403)
            injected = self.config.get('runtime.financial_context')
            if injected and (injected.get('strategy_run_id') != context.strategy_run_id or injected.get('actor_id') != context.actor_id or injected.get('task_id') != context.task_id):
                raise FinancialError('perpl_strategy_run_binding_mismatch',403)
        elif self.config.get('runtime.financial_context'):
            raw = dict(self.config.get('runtime.financial_context'))
            raw['scopes'] = frozenset(raw.get('scopes', []))
            context = FinancialContext(**raw)
        elif self.config.get('runtime.financial_action_id'):
            from nerya.db.sqlite import connect
            con = connect(self.config.paths.db)
            try:
                row = con.execute('SELECT context_json FROM financial_actions WHERE action_id=?',
                                  (self.config.get('runtime.financial_action_id'),)).fetchone()
                if row:
                    raw = json.loads(row['context_json']);raw['scopes'] = frozenset(raw.get('scopes', []))
                    context = FinancialContext(**raw)
            finally:con.close()
        if context and context.task_kind == 'strategy_script':
            context.require('write:trade')
            validate_context(self.config, context, preparing=False)
            if context.plan_only or context.task_id != request['plan'].get('strategy_id') or context.actor_id != 'strategy:' + context.task_id:
                raise FinancialError('perpl_strategy_producer_mismatch', 403)
            return 'strategy_runtime'
        # Model-supplied source must never remove interactive approval requirements.
        return 'operator'

    def _risk(self, request, market):
        if request.get('parameters', {}).get('operation') == 'cancel':
            return {'decision': 'allow', 'scope': 'cancel only; financial approval and account permissions still required'}
        from nerya.sdk.trading_api import _plan_from_dict
        from nerya.trading.submit import _plan_to_intent
        from nerya.trading.risk import RiskGate
        raw = {**request['plan'], 'plan_id': request['plan'].get('plan_id') or 'perpl_' + digest(request)[:24]}
        plan = _plan_from_dict(raw)
        plan.meta = dict(raw.get('meta') or {})
        plan.meta.pop('original_source', None)
        plan.source = self._producer_source(request)
        intent = _plan_to_intent(plan)
        decision = RiskGate(self.config, readonly=True).evaluate(intent, preview=True,
                    market_snapshot={'price': float(unscaled(market['state']['mrk'], market['config']['price_decimals'])),
                                     'timestamp': market['state']['at']['t']/1000, 'age_s': time.time()-market['state']['at']['t']/1000,
                                     '_envelope': {'mode': 'live', 'source': self.api, 'chain_id': self.chain_id}})
        if decision.decision not in {'allow', 'escalate'} or decision.shadow_only:
            raise FinancialError('perpl_risk_gate_' + decision.decision, 403)
        return decision.asdict()

    def quote(self, request):
        _, market = self._context(request)
        wallet, account = self._account()
        order = self._order(request, market, self._positions())
        if order['t'] == 5:
            orders = self._request('GET', '/v1/trading/orders', private=True)
            if not any(o.get('acc') == order['acc'] and o.get('mkt') == order['mkt'] and o.get('oid') == order['oid'] for o in orders.get('d', [])):
                raise FinancialError('perpl_cancel_order_not_owned_or_open', 403)
        risk = self._risk(request, market)
        raw_order = dict(order)
        execution_ceiling = None
        execution_floor = None
        if order['t'] != 5 and order.get('p') == 0:
            factor = Decimal(order['ms'])/10000
            upper = Decimal(max(market['state']['mrk'], market['state']['ask']))*(1+factor)
            lower = Decimal(min(market['state']['mrk'], market['state']['bid']))*(1-factor)
            execution_ceiling = integer(int(upper.to_integral_value(rounding=ROUND_DOWN)), positive=True)
            execution_floor = integer(int(lower.to_integral_value(rounding=ROUND_CEILING)), positive=True)
            # Official API market shape is p=0. These are local quote/reservation bounds;
            # venue ms is relative to its execution-time price, not an absolute price lock.
        size = unscaled(order['s'], market['config']['size_decimals'])
        price = unscaled(max(order.get('p', 0), market['state']['mrk'], market['state']['ask']), market['config']['price_decimals'])
        notional = size * (unscaled(execution_ceiling, market['config']['price_decimals']) if execution_ceiling else price * (1 + Decimal(order.get('ms', 0))/10000))
        rates = market['config'].get('taker_fees', [market['config']['taker_fee']])
        tier = integer(account['ft'])
        if tier >= len(rates):
            raise FinancialError('perpl_fee_tier_unavailable', 422)
        fee = notional * max(Decimal(0), Decimal(rates[tier]))/10**6
        recycle = amount(market['config'].get('recycle_fee', '0'), zero=True)/10**6
        fee += recycle if order['t'] != 5 else Decimal(0)
        margin = notional / (Decimal(order['lv'])/100) if order['t'] in {1, 2} else Decimal(0)
        negative_pnl_budget = notional*Decimal(order.get('mnp',0))/10000 if order['t'] in {1,2} else Decimal(0)
        spend = margin + fee + negative_pnl_budget
        free = max(Decimal(0), amount(account['b'], zero=True)-amount(account['lb'], zero=True))/10**6
        if spend > free:
            raise FinancialError('perpl_insufficient_free_collateral', 403)
        return {'risk_usd': str(notional), 'spend_usd': str(spend), 'fee_usd': str(fee),
                'asset_amounts': {'AUSD': str(spend)}, 'available_asset_amounts': {'AUSD': str(free)},
                'expires_at': min(time.time()+30, market['state']['at']['t']/1000+30, wallet['at']['t']/1000+30), 'perpl_order': order, 'market': market, 'account': account,
                'account_at': wallet['at'], 'risk_decision': risk, 'request_digest': digest(request), 'account_binding': self._binding(), 'recycle_fee_budget_usd': str(recycle), 'raw_order': raw_order, 'negative_pnl_collateral_budget_usd': str(negative_pnl_budget),
                'execution_price_ceiling_scaled': execution_ceiling, 'execution_price_floor_scaled': execution_floor}

    def validate(self, request, quote):
        from nerya.trading.accounts import get_account_profile
        profile = get_account_profile(self.config.paths, self.aid)
        cancel = request.get('parameters', {}).get('operation') == 'cancel'
        permission = profile.permissions.cancel_order if cancel else profile.permissions.place_order
        if profile.venue.lower() != 'perpl' or not profile.is_real_money or not profile.live_trading_enabled or profile.status != 'active' or not permission:
            raise FinancialError('perpl_account_not_live_or_permitted', 403)
        if not self.config.live_trading_enabled() or self.config.kill_switch():
            raise FinancialError('live_trading_disabled', 403)
        if quote.get('expires_at', 0) <= time.time() or quote.get('request_digest') != digest(request) or quote.get('account_binding') != self._binding():
            raise FinancialError('perpl_quote_expired_or_binding_changed', 403)
        _, market = self._context(request)
        _, account = self._account()
        if request.get('parameters', {}).get('operation') == 'cancel':
            orders = self._request('GET', '/v1/trading/orders', private=True)
            self._fresh(orders.get('at', {}))
            if not any(o.get('acc') == self.settings['exchange_account_id'] and o.get('mkt') == quote['perpl_order']['mkt'] and o.get('oid') == quote['perpl_order']['oid'] for o in orders.get('d', [])):
                raise FinancialError('perpl_cancel_order_not_owned_or_open', 403)
        if self._order(request, market, self._positions()) != quote.get('raw_order', quote['perpl_order']):
            raise FinancialError('perpl_order_quote_changed', 409)
        self._risk(request, market)
        ceiling = quote.get('execution_price_ceiling_scaled')
        floor = quote.get('execution_price_floor_scaled')
        if ceiling is not None:
            if max(market['state']['mrk'], market['state']['ask']) > ceiling:
                raise FinancialError('perpl_current_cost_exceeds_frozen_quote', 409)
            if quote['perpl_order']['t'] in {2,3} and min(market['state']['mrk'], market['state']['bid']) < floor:
                raise FinancialError('perpl_current_price_below_frozen_floor', 409)
            notional = unscaled(quote['perpl_order']['s'],market['config']['size_decimals'])*unscaled(ceiling,market['config']['price_decimals'])
        else:
            notional = unscaled(quote['perpl_order']['s'],market['config']['size_decimals'])*unscaled(max(quote['perpl_order'].get('p',0),market['state']['mrk'],market['state']['ask']),market['config']['price_decimals'])
        rates=market['config'].get('taker_fees',[market['config']['taker_fee']])
        tier=integer(account['ft'])
        if tier>=len(rates):raise FinancialError('perpl_fee_tier_unavailable',422)
        fee=notional*max(Decimal(0),Decimal(rates[tier]))/10**6
        if quote['perpl_order']['t']!=5:fee+=amount(market['config'].get('recycle_fee','0'),zero=True)/10**6
        margin=notional/(Decimal(quote['perpl_order']['lv'])/100) if quote['perpl_order']['t'] in {1,2} else Decimal(0)
        negative_pnl_budget=notional*Decimal(quote['perpl_order'].get('mnp',0))/10000 if quote['perpl_order']['t'] in {1,2} else Decimal(0)
        if notional>amount(quote['risk_usd'],zero=True) or fee>amount(quote['fee_usd'],zero=True) or margin+fee+negative_pnl_budget>amount(quote['spend_usd'],zero=True):
            raise FinancialError('perpl_current_cost_exceeds_frozen_quote',409)
        free=max(Decimal(0),amount(account['b'],zero=True)-amount(account['lb'],zero=True))/10**6
        if margin+fee+negative_pnl_budget>free:raise FinancialError('perpl_insufficient_free_collateral',403)
        if quote['expires_at'] <= time.time():
            raise FinancialError('financial_quote_expired', 409)

    def _claim(self, action_id, lfr, order):
        path = self.config.paths.root / 'state' / 'perpl-requests.sqlite'
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path, timeout=15) as con:
            con.execute('CREATE TABLE IF NOT EXISTS requests (action_id TEXT PRIMARY KEY, account TEXT, rq TEXT, order_json TEXT)')
            con.execute('BEGIN IMMEDIATE')
            old = con.execute('SELECT order_json FROM requests WHERE action_id=?', (action_id,)).fetchone()
            if old:
                return json.loads(old[0]), False
            # One persisted counter shared by all keys/processes for this account; coordinate external clients separately.
            identity = str(self.chain_id) + ':' + self.settings['wallet_address'].lower() + ':' + str(order['acc'])
            previous = [int(r[0]) for r in con.execute('SELECT rq FROM requests WHERE account=?', (identity,))]
            rq = integer(max([integer(lfr), *previous])+1, positive=True)
            wire = {**order, 'rq': rq}
            con.execute('INSERT INTO requests VALUES (?,?,?,?)', (action_id, identity, str(rq), json.dumps(wire)))
            return wire, True

    def execute(self, request, quote, submitted):
        try:
            return self._execute(request, quote, submitted)
        except FinancialError as exc:
            # Before _claim there is provably no order identity/network write. Gateway
            # must not misclassify deterministic local denial as an uncertain broadcast.
            aid = self.config.get('runtime.financial_action_id')
            if aid:
                from nerya.db.sqlite import connect
                con = connect(self.config.paths.db)
                try:
                    row = con.execute('SELECT submission_json FROM financial_actions WHERE action_id=?', (aid,)).fetchone()
                    submission = json.loads(row['submission_json']) if row else {}
                finally:con.close()
                ledger = self.config.paths.root / 'state' / 'perpl-requests.sqlite'
                claimed = False
                if ledger.exists():
                    with sqlite3.connect('file:' + str(ledger) + '?mode=ro', uri=True) as counter:
                        claimed = counter.execute('SELECT 1 FROM requests WHERE action_id=?', (aid,)).fetchone() is not None
                if row and not submission and not claimed:
                    return {'state': 'failed_before_submission', 'reason': exc.code, 'pre_submission_denial': True}
            raise

    def _execute(self, request, quote, submitted):
        self.validate(request, quote)
        from nerya.financial.trade_permit import check_trade_permit
        action_id = check_trade_permit(self.config, account_id=self.aid, market=request['market'], notional_usd=quote['risk_usd'])
        if not action_id:
            raise FinancialError('perpl_reserved_gateway_action_required', 403)
        _, current_market = self._context(request)
        current_risk = self._risk(request, current_market)
        if quote.get('risk_decision', {}).get('decision') == 'escalate' or current_risk.get('decision') == 'escalate':
            from nerya.db.sqlite import connect
            con = connect(self.config.paths.db)
            try:
                row = con.execute('SELECT approval_id FROM financial_actions WHERE action_id=?', (action_id,)).fetchone()
                if not row or not row['approval_id']:
                    raise FinancialError('perpl_risk_escalation_requires_action_approval', 403)
            finally:
                con.close()
        # Scope/signer check before claiming identity. No network writes in validation.
        for field in ('api_key_ref', 'private_key_ref'):
            self._vault().resolve(self.settings[field][8:], required_scope='perpl:trade')
        if 'perpl:trade' not in self.settings['approved_scopes']:
            raise FinancialError('perpl_operator_scope_denied', 403)
        _, account = self._account()
        wire, first = self._claim(action_id, account['lfr'], quote['perpl_order'])
        ref = {'chain_id': self.chain_id, 'account_id': wire['acc'], 'market_id': wire['mkt'], 'request_id': wire['rq'],
               'action_id': action_id, 'operation': 'cancel' if wire['t']==5 else 'order', 'order_id': wire.get('oid')}
        submitted(ref)
        if not first:
            return {'state': 'needs_recovery', 'submission': ref, 'reason': 'existing_request_never_resubmitted'}
        response = self._request('POST', '/v1/trading/orders', {'mt': 30, 'sn': wire['rq'], 'd': [wire]}, private=True, trade=True)
        if response.get('mt') != 31 or response.get('cid') != wire['rq']:
            return {'state': 'needs_recovery', 'submission': ref, 'reason': 'uncorrelated_batch_response'}
        statuses = response.get('statuses')
        code = response.get('status', {}).get('code')
        if code == 0:
            if not isinstance(statuses, list) or len(statuses) != 1:
                return {'state': 'needs_recovery', 'submission': ref, 'reason': 'missing_per_order_status'}
            code = statuses[0].get('code')
        if code in {400, 403, 429, 503}:
            return {'state': 'rejected', 'submission': ref, 'reason': 'perpl_admission_rejected', 'provider_code': code}
        return {'state': 'submitted' if code == 0 else 'needs_recovery', 'submission': ref, 'admission_only': True}

    def cancel(self, request, quote, submitted):
        if request.get('parameters', {}).get('operation') != 'cancel':
            raise FinancialError('perpl_cancel_plan_required', 400)
        return self.execute(request, quote, submitted)

    def status(self, request, quote, submission):
        if submission.get('chain_id') != self.chain_id or submission.get('account_id') != self.settings['exchange_account_id'] or submission.get('market_id') != quote['perpl_order']['mkt']:
            raise FinancialError('perpl_submission_identity_mismatch', 403)
        if quote.get('account_binding') != self._binding():
            raise FinancialError('perpl_recovery_account_binding_changed', 403)
        self._account(recovery=True)
        orders = self._request('GET', '/v1/trading/orders', private=True).get('d', [])
        page = ''
        seen = set()
        for _ in range(20):
            doc = self._request('GET', '/v1/trading/order-history?' + urlencode({'count': 100, **({'page': page} if page else {})}), private=True)
            orders.extend(doc.get('d', []))
            page = doc.get('np')
            if not page or page in seen:
                break
            seen.add(page)
        matches = [o for o in orders if o.get('rq') == submission.get('request_id') and o.get('acc') == submission['account_id'] and o.get('mkt') == submission['market_id']]
        if not matches:
            return {'state': 'needs_recovery', 'reason': 'original_request_not_observed_no_resubmit', 'submission': submission}
        order = max(matches, key=lambda o: (o.get('at', {}).get('t', 0), o.get('at', {}).get('b', 0), o.get('at', {}).get('l', 0)))
        state, filled = order.get('st'), integer(order.get('fs', 0))
        result = {'submission': submission, 'order': order, 'provider_status': state, 'provider_reason': order.get('sr'), 'provider_failure': order.get('fr'),
                  'provider_reason_name': {44: 'TakerOrderSettlementFailed', 23: 'MakerOrderSettlementFailed', 36: 'OrderPostFailed'}.get(order.get('sr')),
                  'provider_failure_name': {8: 'ExceedsMaxNegPnlCollat', 7: 'ReferencePriceStale', 1: 'InsufficientBalance', 4: 'NoPositionToClose'}.get(order.get('fr')), 'partial': bool(filled and filled < order.get('os', filled))}
        if state in {4, 5, 6, 7, 10}:
            if filled:
                # No fabricated settlement or fill receipts from admission. Require actual order accounting.
                if not order.get('fp') or 'f' not in order or not order.get('at', {}).get('txid'):
                    return {**result, 'state': 'needs_recovery', 'reason': 'fill_accounting_or_chain_receipt_missing'}
                tx_hash = transaction_hash(order['at']['txid'])
                rpc_status, receipt = self.http.request('POST', self.rpc, headers={'Content-Type': 'application/json'},
                    body=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'eth_getTransactionReceipt', 'params': [tx_hash]}).encode())
                chain_receipt = receipt.get('result')
                if rpc_status != 200 or not isinstance(chain_receipt, dict) or chain_receipt.get('transactionHash', '').lower() != tx_hash or chain_receipt.get('status') != '0x1':
                    return {**result, 'state': 'needs_recovery', 'reason': 'chain_execution_not_confirmed'}
                size = unscaled(filled, quote['market']['config']['size_decimals'])
                price = unscaled(order['fp'], quote['market']['config']['price_decimals'])
                notional = size*price
                fee = amount(order['f'], zero=True)/10**6
                breaches = []
                if notional > amount(quote['risk_usd'], zero=True):
                    breaches.append('settled_notional_exceeds_reserved_risk')
                if fee > amount(quote['fee_usd'], zero=True):
                    breaches.append('settled_fee_exceeds_reserved_fee')
                if filled > quote['perpl_order']['s']:
                    breaches.append('filled_size_exceeds_requested_size')
                ceiling, floor = quote.get('execution_price_ceiling_scaled'), quote.get('execution_price_floor_scaled')
                if ceiling is not None and order['fp'] > ceiling:
                    breaches.append('fill_price_exceeds_frozen_upper_bound')
                if floor is not None and quote['perpl_order']['t'] in {2,3} and order['fp'] < floor:
                    breaches.append('fill_price_below_frozen_lower_bound')
                # Actual execution is preserved even on a budget breach. Never release its
                # reservation as rejected or label it cleanly confirmed; operator must reconcile.
                return {**result, 'state': 'needs_recovery' if breaches else 'confirmed',
                        'execution_confirmed': True, 'budget_breaches': breaches,
                        'operator_action_required': bool(breaches),
                        **({'reason': 'actual_execution_exceeds_frozen_budget'} if breaches else {}),
                        'filled_size': str(size), 'average_price': str(price),
                        'transaction_hash': tx_hash, 'settled_notional_usd': str(notional),
                        'settled_fee_usd': str(fee)}
            if submission['operation'] == 'cancel' and state in {5, 10} and order.get('sr') == 28:
                return {**result, 'state': 'confirmed', 'canceled': True, 'settled_notional_usd': '0', 'settled_fee_usd': '0'}
            return {**result, 'state': 'rejected'}
        return {**result, 'state': 'confirming'}


PARAMETERS = {'type': 'object', 'properties': {
    'max_negative_pnl_bps': {'type': 'integer', 'minimum': 0, 'maximum': 100},
    'operation': {'enum': ['order', 'cancel']}, 'reduce_only': {'type': 'boolean'},
    'slippage_bps': {'type': 'integer', 'minimum': 1, 'maximum': 1000},
    'position_id': {'type': 'integer', 'minimum': 1}, 'order_id': {'type': 'integer', 'minimum': 1}},
    'additionalProperties': False}
TRADING_COMPONENTS = (TradingComponentSpec('perpl', '1.0.0', ('trade',), ('linear',),
                       lambda config, request: PerplTransport(config, request),
                       parameters_schema=PARAMETERS, account_state=True, cancel=True, trade_transport='onchain_perpetual'),)
