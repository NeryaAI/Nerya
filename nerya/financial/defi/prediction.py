"""Bounded, strategy-owned binary CTF redemption through the pUSD adapter.

Only reviewed standard CTF markets and an owned EOA are enabled here. Deposit
wallets, Safe/proxy relaying, negative-risk and combo position managers require
their own verified transports. Never call SDK helpers that provision wallets
or silently issue setApprovalForAll.
https://docs.polymarket.com/trading/positions/manage
"""
from contextlib import closing
from decimal import Decimal

from eth_abi import decode
from eth_utils import keccak

from ..contracts import FinancialError
from .calls import ZERO, address, calldata, transaction
from .transport import ProtocolFunds

REDEMPTION_SCHEMA = {'type':'object', 'properties': {
    'condition_id': {'type':'string','pattern':'^0x[a-fA-F0-9]{64}$'},
    'strategy_id': {'type':'string','pattern':'^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$'}},
    'required':['condition_id','strategy_id'], 'additionalProperties':False}


def outcome_debits(logs, ctf, owner, receiver):
    """Read exact ERC1155 debits, including TransferBatch. No token-id guessing."""
    single = '0x' + keccak(text='TransferSingle(address,address,address,uint256,uint256)').hex()
    batch = '0x' + keccak(text='TransferBatch(address,address,address,uint256[],uint256[])').hex()
    result = {}
    for log in logs:
        topics = log.get('topics') or []
        if str(log.get('address','')).lower() != ctf.lower() or len(topics) != 4 or topics[0].lower() not in {single,batch}:
            continue
        source, target = '0x'+topics[2][-40:], '0x'+topics[3][-40:]
        if source.lower() != owner.lower():
            continue
        if target.lower() not in {receiver.lower(), ZERO}:
            raise FinancialError('redemption_unexpected_outcome_recipient',403)
        raw = bytes.fromhex(log['data'][2:])
        if topics[0].lower() == single:
            token, quantity = decode(['uint256','uint256'], raw)
            entries = [(token,quantity)]
        else:
            tokens, quantities = decode(['uint256[]','uint256[]'], raw)
            if len(tokens) != len(quantities):
                raise FinancialError('redemption_malformed_outcome_transfer',503)
            entries = zip(tokens, quantities)
        for token, quantity in entries:
            result[str(token)] = result.get(str(token),0) + quantity
    return result


class PredictionRedemptionFunds(ProtocolFunds):
    def __init__(self, config, request, **kwargs):
        if request.get('protocol') != 'polymarket_ctf' or request['kind'] != 'redeem' or request.get('chain') != 'polygon':
            raise FinancialError('unsupported_prediction_settlement',422)
        super().__init__(config, request, **kwargs)
        self.ctf, self.adapter, self.collateral = (address(self.deployment['contracts'].get(name))
            for name in ('conditional_tokens','collateral_adapter','collateral'))
        if self.connector.get_erc20_decimals(self.collateral) != 6:
            raise FinancialError('prediction_collateral_decimals_mismatch',403)

    def _identity(self, request):
        from ...trading.accounts import get_account_profile
        profile = get_account_profile(self.config.paths, request.get('account_id'))
        raw = profile.raw
        options = {**raw.get('options',{}), **raw.get('provider_config',{})}
        funder = options.get('funder') or raw.get('funder')
        signature_type = options.get('signature_type', raw.get('signature_type',0))
        if profile.kind != 'prediction_market' or profile.wallet_id != request['wallet_id']:
            raise FinancialError('redemption_prediction_wallet_binding_required',403)
        if not profile.is_real_money or not profile.live_trading_enabled or profile.status != 'active':
            raise FinancialError('redemption_prediction_account_not_live',403)
        if str(signature_type) != '0' or str(funder or '').lower() != self.sender.lower():
            raise FinancialError('redemption_requires_owned_eoa_not_proxy',422)
        sid = request['parameters']['strategy_id']
        owner = self.owner()
        if owner.startswith('strategy:') and owner != 'strategy:'+sid:
            raise FinancialError('redemption_strategy_owner_mismatch',403)
        if not owner.startswith('strategy:'):
            from ...strategies.package import load_package
            package = load_package(self.config.paths, sid)
            if request['account_id'] not in package.manifest.accounts:
                raise FinancialError('redemption_strategy_account_mismatch',403)
        return sid

    def _condition(self, request, block='latest'):
        cid = request['parameters']['condition_id']
        spec = (self.deployment.get('conditions') or {}).get(cid)
        if not spec or spec.get('reviewed') is not True or spec.get('negative_risk') is not False:
            raise FinancialError('redemption_standard_condition_review_required',422)
        condition = bytes.fromhex(cid[2:])
        denominator = self.call(self.ctf, 'payoutDenominator(bytes32)', ['bytes32'],[condition],['uint256'],block)[0]
        numerators = [self.call(self.ctf,'payoutNumerators(bytes32,uint256)',['bytes32','uint256'],[condition,i],['uint256'],block)[0] for i in (0,1)]
        if not denominator or sum(numerators) != denominator:
            raise FinancialError('prediction_condition_not_resolved',409)
        if self.call(self.ctf,'getOutcomeSlotCount(bytes32)',['bytes32'],[condition],['uint256'],block)[0] != 2:
            raise FinancialError('redemption_requires_binary_condition',422)
        tokens = []
        for index in (1,2):
            collection = self.call(self.ctf,'getCollectionId(bytes32,bytes32,uint256)',
                ['bytes32','bytes32','uint256'],[bytes(32),condition,index],['bytes32'],block)[0]
            token = self.call(self.ctf,'getPositionId(address,bytes32)',['address','bytes32'],
                [address(spec['outcome_collateral']),collection],['uint256'],block)[0]
            tokens.append(str(token))
        if tokens != [str(value) for value in spec.get('token_ids',[])]:
            raise FinancialError('redemption_condition_token_mismatch',403)
        return condition, tokens, numerators, denominator

    def _plan(self, request):
        sid = self._identity(request)
        condition, tokens, numerators, denominator = self._condition(request)
        approved = self.call(self.ctf,'isApprovedForAll(address,address)',['address','address'],
                             [self.sender,self.adapter],['bool'])[0]
        if not approved:
            raise FinancialError('redemption_existing_operator_approval_required',403)
        from ...trading.position_book import PositionBook
        from ...trading.order_tracker import OrderTracker
        with closing(OrderTracker(self.config.paths)) as tracker:
            if tracker.active_orders(account_id=request['account_id']):
                raise FinancialError('redemption_requires_no_unresolved_account_orders',403)
        balances = []
        with closing(PositionBook(self.config.paths)) as book:
            for token in tokens:
                balance = self.call(self.ctf,'balanceOf(address,uint256)',['address','uint256'],[self.sender,int(token)],['uint256'])[0]
                share = book.get_share(account_id=request['account_id'],strategy_id=sid,market='POLYMARKET:'+token)
                owned = Decimal(str(share.size_share_base if share else 0))*1_000_000
                if owned != balance:
                    raise FinancialError('redemption_full_balance_not_owned_by_strategy',403)
                balances.append(balance)
        if not any(balances):
            raise FinancialError('prediction_no_redeemable_positions',409)
        payouts = [balance * numerator // denominator for balance,numerator in zip(balances,numerators)]
        data = calldata('redeemPositions(address,bytes32,bytes32,uint256[])',
            ['address','bytes32','bytes32','uint256[]'],[self.collateral,bytes(32),condition,[1,2]])
        return {'strategy_id':sid,'token_ids':tokens,'balances_base':[str(x) for x in balances],
                'payouts_base':[str(x) for x in payouts], 'expected_payout_base':str(sum(payouts)),
                'transaction':transaction(self.sender,self.adapter,data)}

    def quote(self, request):
        plan = self._plan(request)
        face_value = sum((Decimal(value)/1_000_000 for value in plan['balances_base']),Decimal(0))
        quote = self.transaction_quote(request,plan['transaction'],risk_usd=face_value,spend_assets={},prices={})
        return {**quote, 'redemption':plan, 'collateral':self.collateral, 'collateral_symbol':'PUSD',
                'valuation_basis':'PUSD nominal; not a verified USD conversion',
                'exposure_asset_amounts':{'POLYMARKET:'+token:str(Decimal(value)/1_000_000)
                    for token,value in zip(plan['token_ids'],plan['balances_base'])}}

    def validate(self, request, quote):
        if self._plan(request) != quote['redemption']:
            raise FinancialError('redemption_position_changed_after_quote',403)
        super().validate(request, quote)

    def _book(self, request, quote, result, action_id):
        from ...trading.order_tracker import OrderTracker
        from ...trading.position_book import PositionBook
        plan = quote['redemption']
        total = sum(Decimal(value) for value in plan['balances_base'])
        for token, quantity, payout in zip(plan['token_ids'],plan['balances_base'],plan['payouts_base']):
            quantity, payout = Decimal(quantity)/1_000_000, Decimal(payout)/1_000_000
            if not quantity:
                continue
            fee = float(Decimal(result['settled_fee_usd'])*quantity*1_000_000/total)
            market = 'POLYMARKET:'+token
            # The chain effect, not a retry's action id, owns the accounting.
            cid = 'redeem_' + result['transaction_hash'] + '_' + token
            with closing(OrderTracker(self.config.paths)) as tracker:
                order = tracker.get_by_client_order_id(cid)
                if order is not None and (order.account_id, order.strategy_id) != (request['account_id'], plan['strategy_id']):
                    raise FinancialError('redemption_receipt_already_owned',403)
                if order is None:
                    order = tracker.register(client_order_id=cid, account_id=request['account_id'],
                        strategy_id=plan['strategy_id'],market=market,side='sell',order_type='market',
                        size_base=float(quantity),notional_usd=float(payout),
                        meta={'operation':'redemption','transaction_hash':result['transaction_hash'],'collateral':'PUSD'})
                tracker.mark_submitted(order.order_id,exchange_order_id=result['transaction_hash'])
                fill = tracker.record_fill(order_id=order.order_id,price=float(payout/quantity),size_base=float(quantity),
                    fee_usd=fee,source='live',cumulative_filled=float(quantity),meta={'operation':'redemption'})
                if fill is None:
                    fills = tracker.fills_for_order(order.order_id)
                    fill = fills[0] if fills else None
                if fill is None:
                    raise FinancialError('redemption_fill_accounting_missing',503)
                with closing(PositionBook(self.config.paths)) as book:
                    book.apply_fill(account_id=request['account_id'],strategy_id=plan['strategy_id'],market=market,
                        side='sell',price=float(payout/quantity),size_base=float(quantity),fee_usd=fill.fee_usd,
                        source='live',order_id=order.order_id,fill_id=fill.fill_id)
                tracker.update_state(order.order_id,'filled')

    def status(self, request, quote, submission):
        result, receipt = self.verified_receipt(quote, submission)
        if receipt is None:
            return result
        try:
            plan = quote['redemption']
            debits = outcome_debits(receipt.get('logs') or [], self.ctf, self.sender, self.adapter)
            expected = {token:int(value) for token,value in zip(plan['token_ids'],plan['balances_base']) if int(value)}
            if debits != expected:
                raise FinancialError('redemption_outcome_debits_mismatch',503)
            from ...wallet.receipts import token_received_base
            payout = token_received_base(receipt,self.collateral,self.sender)
            if payout is None or payout != int(plan['expected_payout_base']):
                raise FinancialError('redemption_collateral_payout_mismatch',503)
            for token in plan['token_ids']:
                if self.call(self.ctf,'balanceOf(address,uint256)',['address','uint256'],[self.sender,int(token)],['uint256'],receipt['blockNumber'])[0]:
                    raise FinancialError('redemption_outcome_balance_not_cleared',503)
            aid = submission.get('action_id')
            if not aid:
                raise FinancialError('protocol_action_identity_required',403)
            self._book(request,quote,result,aid)
            return {'state':'confirmed',**result,'settled_notional_usd':str(Decimal(plan['expected_payout_base'])/1_000_000),
                    'payout_asset':'PUSD','payout_base':plan['expected_payout_base'],'outcome_debits':debits,'strategy_id':plan['strategy_id']}
        except FinancialError as exc:
            return {'state':'needs_recovery',**result,'reason':exc.code}
