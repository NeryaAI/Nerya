"""Owned Uniswap v3/v4 ERC20 position lifecycles, without implicit approvals."""
import time
from decimal import Decimal

from eth_abi import encode
from eth_utils import keccak

from ...trading.portfolio_risk import RiskMetrics,evaluate
from ..adapters import units
from ..contracts import FinancialError
from .book import ProtocolBook
from .calls import address,ZERO,POOL_KEY,v3_call,v4_call,nft_mint_id,v3_liquidity_receipt,v3_collect_receipt,v4_receipt,topic_address
from .liquidity import token_amounts,pool_price,position_ticks
from .transport import ProtocolFunds

LP_ACTIONS=("lp_add","lp_remove","lp_collect")
LP_SCHEMA={"type":"object","properties":{
    "pool_id":{"type":"string","minLength":1},"token_id":{"type":"integer","minimum":1},
    "tick_lower":{"type":"integer","minimum":-887272,"maximum":887272},"tick_upper":{"type":"integer","minimum":-887272,"maximum":887272},
    **{key:{"type":"string","pattern":r"^\d+(\.\d+)?$"} for key in ("amount0_max","amount1_max","amount0_min","amount1_min","collect0_max","collect1_max")},
    "liquidity":{"type":"integer","minimum":1,"maximum":2**127-1},"minimum_liquidity":{"type":"integer","minimum":1,"maximum":2**127-1}},
    "required":["pool_id"],"additionalProperties":False}


class UniswapFunds(ProtocolFunds):
    supported_protocols = frozenset({'uniswap_v3', 'uniswap_v4'})
    v3_protocol_fee_type = 'uint8'

    def __init__(self,config,request,**kwargs):
        if request.get("protocol") not in self.supported_protocols or request["kind"] not in LP_ACTIONS:
            raise FinancialError("unsupported_lp_operation",422)
        super().__init__(config,request,**kwargs)
        self.v4=self.protocol=="uniswap_v4"
        self.manager=address(self.deployment["contracts"].get("position_manager"))
        self.book=ProtocolBook(config)

    def _pool(self,pool_id,block="latest"):
        raw=(self.deployment.get("pools") or {}).get(pool_id)
        if not raw or raw.get("reviewed") is not True:raise FinancialError("lp_pool_not_reviewed",422)
        a,b=address(raw["token0"]),address(raw["token1"])
        if int(a,16)>=int(b,16):raise FinancialError("tokens_must_be_sorted",422)
        fee,spacing=int(raw["fee"]),int(raw["tick_spacing"])
        if self.v4:
            hooks=str(raw.get("hooks") or ZERO)
            if hooks.lower()!=ZERO:raise FinancialError("v4_hook_execution_not_reviewed",422)
            key=(a,b,fee,spacing,ZERO);pid="0x"+keccak(encode([POOL_KEY],[key])).hex()
            state_view=address(self.deployment["contracts"].get("state_view"))
            slot=self.call(state_view,"getSlot0(bytes32)",["bytes32"],[bytes.fromhex(pid[2:])],["uint160","int24","uint24","uint24"],block)
            sqrt,tick=slot[:2]
            if slot[3]!=fee:raise FinancialError("lp_dynamic_fee_requires_dedicated_model",422)
        else:
            factory=address(self.deployment["contracts"].get("factory"))
            pool=self.call(factory,"getPool(address,address,uint24)",["address","address","uint24"],[a,b,fee],["address"],block)[0]
            if pool.lower()!=str(raw.get("address")).lower():raise FinancialError("lp_pool_identity_changed",403)
            code=self.connector._rpc("eth_getCode",[pool,block])
            if "0x"+keccak(bytes.fromhex(code[2:])).hex()!=raw.get("code_hash"):raise FinancialError("lp_pool_code_changed",403)
            if self.call(pool,"tickSpacing()",[],[],["int24"],block)[0]!=spacing:raise FinancialError("lp_tick_spacing_changed",403)
            slot=self.call(pool,"slot0()",[],[],["uint160","int24","uint16","uint16","uint16",self.v3_protocol_fee_type,"bool"],block)
            sqrt,tick=slot[:2];key=None;pid=pool
        if not sqrt:raise FinancialError("lp_pool_not_initialized",422)
        decimals=[self.connector.get_erc20_decimals(token) for token in (a,b)]
        prices=[self._price(token,block) for token in (a,b)]
        observed=pool_price(sqrt,*decimals);fair=prices[0]/prices[1]
        tolerance=self.config.get("financial.defi.max_pool_deviation_bps")
        if type(tolerance) is not int or not 0<tolerance<=1000:raise FinancialError("lp_price_deviation_policy_required",422)
        if abs(observed-fair)>fair*Decimal(tolerance)/10000:raise FinancialError("lp_pool_price_deviation",403)
        return {"pool_id":pool_id,"id":pid,"key":key,"token0":a,"token1":b,"fee":fee,"tick_spacing":spacing,
                "sqrt_price_x96":sqrt,"tick":tick,"decimals":decimals,"prices_usd":[str(item) for item in prices]}

    def _price(self,token,block="latest"):
        feed=(self.deployment.get("price_feeds") or {}).get(token.lower())
        if not feed or feed.get("reviewed") is not True:raise FinancialError("lp_asset_price_feed_not_reviewed",422)
        target=address(feed["address"])
        if target.lower() not in {value.lower() for value in self.deployment["contracts"].values()}:
            raise FinancialError("lp_price_feed_code_evidence_missing",422)
        rid,answer,_,updated,answered=self.call(target,"latestRoundData()",[],[],["uint80","int256","uint256","uint256","uint80"],block)
        if answer<=0 or answered<rid or (block=="latest" and not 0<=time.time()-updated<=int(feed["heartbeat_seconds"])):
            raise FinancialError("lp_price_feed_stale",503)
        decimals=self.call(target,"decimals()",[],[],["uint8"],block)[0]
        return Decimal(answer)/10**decimals

    def _position(self,token_id,block="latest"):
        owner=self.call(self.manager,"ownerOf(uint256)",["uint256"],[token_id],["address"],block)[0]
        if owner.lower()!=self.sender.lower():raise FinancialError("lp_nft_not_owned",403)
        if self.v4:
            key,info=self.call(self.manager,"getPoolAndPositionInfo(uint256)",["uint256"],[token_id],[POOL_KEY,"uint256"],block)
            lower,upper=position_ticks(info)
            liquidity=self.call(self.manager,"getPositionLiquidity(uint256)",["uint256"],[token_id],["uint128"],block)[0]
            identity=(key[0].lower(),key[1].lower(),key[2],key[3],key[4].lower())
            pool_id=next((name for name,value in (self.deployment.get("pools") or {}).items() if
                (str(value["token0"]).lower(),str(value["token1"]).lower(),value["fee"],value["tick_spacing"],str(value.get("hooks") or ZERO).lower())==identity),None)
            fees=None
        else:
            data=self.call(self.manager,"positions(uint256)",["uint256"],[token_id],
                ["uint96","address","address","address","uint24","int24","int24","uint128","uint256","uint256","uint128","uint128"],block)
            _,_,a,b,fee,lower,upper,liquidity,last0,last1,owed0,owed1=data
            pool_id=next((name for name,value in (self.deployment.get("pools") or {}).items() if
                (str(value["token0"]).lower(),str(value["token1"]).lower(),value["fee"])==(a.lower(),b.lower(),fee)),None)
            fees=[str(owed0),str(owed1)]
        if pool_id is None:raise FinancialError("lp_inventory_contains_unreviewed_pool",422)
        pool=self._pool(pool_id,block)
        if self.v4:
            view=address(self.deployment["contracts"]["state_view"]);pid=bytes.fromhex(pool["id"][2:])
            size,last0,last1=self.call(view,"getPositionInfo(bytes32,address,int24,int24,bytes32)",
                ["bytes32","address","int24","int24","bytes32"],[pid,self.manager,lower,upper,token_id.to_bytes(32,"big")],
                ["uint128","uint256","uint256"],block)
            if size!=liquidity:raise FinancialError("lp_liquidity_state_mismatch",503)
            growth=self.call(view,"getFeeGrowthInside(bytes32,int24,int24)",["bytes32","int24","int24"],[pid,lower,upper],["uint256","uint256"],block)
            fees=[str(((value-last)%(2**256))*liquidity//2**128) for value,last in zip(growth,(last0,last1))]
        elif liquidity:
            globals_=[self.call(pool["id"],f"feeGrowthGlobal{index}X128()",[],[],["uint256"],block)[0] for index in (0,1)]
            rows=[self.call(pool["id"],"ticks(int24)",["int24"],[tick],
                ["uint128","int128","uint256","uint256","int56","uint160","uint32","bool"],block) for tick in (lower,upper)]
            current=pool["tick"]
            calculated=[]
            for index,global_ in enumerate(globals_):
                below=rows[0][2+index] if current>=lower else (global_-rows[0][2+index])%(2**256)
                above=rows[1][2+index] if current<upper else (global_-rows[1][2+index])%(2**256)
                inside=(global_-below-above)%(2**256)
                calculated.append(str(int(fees[index])+((inside-(last0,last1)[index])%(2**256))*liquidity//2**128))
            fees=calculated
        amounts=token_amounts(liquidity,pool["sqrt_price_x96"],lower,upper)
        value=sum((quantity/10**decimals*Decimal(price) for quantity,decimals,price in zip(amounts,pool["decimals"],pool["prices_usd"])),Decimal(0))
        owed=sum((Decimal(quantity)/10**decimals*Decimal(price) for quantity,decimals,price in zip(fees,pool["decimals"],pool["prices_usd"])),Decimal(0))
        return {"token_id":token_id,"owner":owner,"pool":pool,"tick_lower":lower,"tick_upper":upper,"liquidity":str(liquidity),
                "amounts_base":[str(item) for item in amounts],"principal_value_usd":str(value),"position_value_usd":str(value+owed),
                "tokens_owed_base":fees,"owed_value_usd":str(owed),"closed":not liquidity and not any(int(item) for item in fees)}

    def _inventory(self):
        count=self.call(self.manager,"balanceOf(address)",["address"],[self.sender],["uint256"])[0]
        maximum=int(self.config.get("financial.defi.max_lp_positions",100))
        if count>maximum:raise FinancialError("lp_inventory_size_exceeds_limit",422)
        if count==0:return []
        ids={int(row["state"]["token_id"]) for row in self.book.positions(chain=self.chain,protocol=self.protocol,owner_address=self.sender)}
        if len(ids)!=count:
            start=self.deployment.get("start_block")
            if type(start) is not int or start<0:raise FinancialError("lp_inventory_coverage_required",422)
            latest=self.connector.get_block_number();ids=set()
            topic="0x"+keccak(text="Transfer(address,address,uint256)").hex()
            for beginning in range(start,latest+1,5000):
                logs=self.connector._rpc("eth_getLogs",[{"address":self.manager,"fromBlock":hex(beginning),"toBlock":hex(min(latest,beginning+4999)),
                    "topics":[topic,None,topic_address(self.sender)]}])
                if not isinstance(logs,list):raise FinancialError("lp_inventory_evidence_unavailable",503)
                ids.update(int(log["topics"][3],16) for log in logs if len(log.get("topics",[]))==4)
                if len(ids)>maximum:raise FinancialError("lp_inventory_size_exceeds_limit",422)
        positions=[]
        for token_id in sorted(ids):
            owner=self.call(self.manager,"ownerOf(uint256)",["uint256"],[token_id],["address"])[0]
            if owner.lower()==self.sender.lower():positions.append(self._position(token_id))
        if len(positions)!=count:raise FinancialError("lp_inventory_reconciliation_required",503)
        return positions

    def _key(self,token_id):return f"{self.chain_spec.chain_id}:{self.protocol}:{self.manager.lower()}:{token_id}"

    def _owned(self,token_id,owner):
        position=self._position(token_id)
        claims,total=self.book.claims(self._key(token_id),owner)
        liquidity=Decimal(position["liquidity"])
        if "liquidity" not in claims:raise FinancialError("lp_position_not_owned_by_strategy",403)
        if total.get("liquidity",Decimal(0))!=liquidity:raise FinancialError("lp_claim_reconciliation_required",403)
        if claims.get("liquidity",Decimal(0))!=liquidity:raise FinancialError("lp_nft_requires_sole_strategy_owner",403)
        return position

    def _plan(self,request):
        p=dict(request.get("parameters") or {});pool=self._pool(p["pool_id"]);owner=self.owner()
        token_id=p.get("token_id")
        if request.get("position_id") and request["position_id"]!=str(token_id):raise FinancialError("lp_position_id_mismatch",403)
        position=self._owned(token_id,owner) if token_id else None
        if position and position["pool"]["pool_id"]!=pool["pool_id"]:raise FinancialError("lp_pool_position_mismatch",403)
        if not token_id and request["kind"]!="lp_add":raise FinancialError("lp_position_required",400)
        lower=p.get("tick_lower",position["tick_lower"] if position else None)
        upper=p.get("tick_upper",position["tick_upper"] if position else None)
        if position and (lower,upper)!=(position["tick_lower"],position["tick_upper"]):raise FinancialError("lp_existing_range_immutable",403)
        wire={"token0":pool["token0"],"token1":pool["token1"],"fee":pool["fee"],"tick_lower":lower,"tick_upper":upper,
              "deadline":int(time.time())+30,**({"token_id":token_id} if token_id else {})}
        for number,decimals in enumerate(pool["decimals"]):
            for bound in ("max","min"):
                wire[f"amount{number}_{bound}"]=units(p.get(f"amount{number}_{bound}","0"),decimals)
            wire[f"collect{number}_max"]=units(p.get(f"collect{number}_max","0"),decimals)
        if request["kind"]=="lp_add":
            if not wire["amount0_max"] and not wire["amount1_max"]:raise FinancialError("lp_input_amount_required",400)
            if any(wire[f"amount{n}_max"] and not wire[f"amount{n}_min"] for n in (0,1)):
                raise FinancialError("lp_nonzero_minimum_inputs_required",400)
            operation="increase" if token_id else "mint"
            if self.v4:wire["liquidity"]=p.get("liquidity")
            minimum=p.get("minimum_liquidity")
            if type(minimum) is not int or minimum<=0:raise FinancialError("lp_minimum_liquidity_required",400)
            if self.v4 and (type(wire["liquidity"]) is not int or minimum>wire["liquidity"]):raise FinancialError("lp_minimum_liquidity_conflict",400)
        else:
            operation="collect" if request["kind"]=="lp_collect" else "decrease" if self.v4 else "decrease_collect"
            if request["kind"]=="lp_remove":
                quantity=p.get("liquidity")
                if type(quantity) is not int or not 0<quantity<=int(position["liquidity"]):raise FinancialError("lp_exit_exceeds_owned_liquidity",403)
                wire["liquidity"]=quantity
            if not self.v4 and not (wire["collect0_max"] or wire["collect1_max"]):raise FinancialError("lp_collect_bounds_required",400)
        tx=v4_call(self.manager,self.sender,operation,pool["key"],wire) if self.v4 else v3_call(self.manager,self.sender,operation,wire,spacing=pool["tick_spacing"])
        return pool,position,operation,wire,tx,owner

    def account_state(self,request):
        inventory=self._inventory()
        return {"source":self.protocol+":"+self.manager,"as_of":time.time(),"health":"ok","coverage":["lp_nft_inventory","liquidity","ranges","principal"],
                "positions":inventory,"fees_coverage":"fee_growth_inside"}

    def quote(self,request):
        pool,position,operation,wire,tx,owner=self._plan(request)
        inventory=self._inventory()
        spend={};prices={token:Decimal(value) for token,value in zip((pool["token0"],pool["token1"]),pool["prices_usd"])}
        prerequisites=[]
        if operation in {"mint","increase"}:
            for index,token in enumerate((pool["token0"],pool["token1"])):
                quantity=wire[f"amount{index}_max"]
                if not quantity:continue
                spend[token]=str(Decimal(quantity)/10**pool["decimals"][index])
                spender=address(self.deployment["contracts"].get("permit2")) if self.v4 else self.manager
                allowance=self.call(token,"allowance(address,address)",["address","address"],[self.sender,spender],["uint256"])[0]
                if allowance<quantity:prerequisites.append({"kind":"contract_approval","wallet_id":request["wallet_id"],"chain":self.chain,"asset":token,"amount":spend[token],"spender":spender})
                if self.v4:
                    allowed,expiration,_=self.call(spender,"allowance(address,address,address)",["address"]*3,[self.sender,token,self.manager],["uint160","uint48","uint48"])
                    if allowed<quantity or expiration<wire["deadline"]:
                        maximum=self.config.get("financial.defi.max_permit2_seconds")
                        if type(maximum) is not int or maximum<60:raise FinancialError("permit2_expiry_limit_required",422)
                        prerequisites.append({"kind":"permit2_approval","wallet_id":request["wallet_id"],"chain":self.chain,"protocol":self.protocol,
                            "asset":token,"amount":spend[token],"spender":self.manager,"parameters":{"expiration":int(time.time())+maximum}})
        lp_value=sum((Decimal(row["position_value_usd"]) for row in inventory),Decimal(0))
        movement=sum((Decimal(quantity)*prices[token] for token,quantity in spend.items()),Decimal(0))
        exiting=Decimal(position["principal_value_usd"])*Decimal(wire.get("liquidity",0))/Decimal(position["liquidity"]) if operation in {"decrease","decrease_collect"} else Decimal(0)
        if operation=="collect":
            exiting=sum((Decimal(int(quantity) if self.v4 else min(int(quantity),wire[f"collect{index}_max"]))/10**pool["decimals"][index]*Decimal(pool["prices_usd"][index])
                         for index,quantity in enumerate(position["tokens_owed_base"])),Decimal(0))
        risk=movement if spend else exiting if operation in {"decrease","decrease_collect"} else Decimal(position["owed_value_usd"])
        quote=self.transaction_quote(request,tx,risk_usd=risk,spend_assets=spend,prices=prices,prerequisites=prerequisites)
        # Protocol exposure is separate from the underlying tokens returned
        # on exit. Removing LP risk does not assert that ETH became cash USD.
        wallet_value=Decimal(0)
        for token,decimals,price in zip((pool["token0"],pool["token1"]),pool["decimals"],pool["prices_usd"]):
            balance=self.call(token,"balanceOf(address)",["address"],[self.sender],["uint256"])[0]
            wallet_value+=Decimal(balance)/10**decimals*Decimal(price)
        equity=lp_value+wallet_value
        before=RiskMetrics(equity,gross_exposure_usd=equity,stress_loss_usd=equity+lp_value,protocol_exposure_usd=lp_value)
        after=RiskMetrics(equity,gross_exposure_usd=equity,stress_loss_usd=equity+lp_value+movement-exiting,
                          protocol_exposure_usd=max(Decimal(0),lp_value+movement-exiting))
        limits=self.config.get("financial.portfolio_limits",{}).get("wallet:"+request["wallet_id"],{})
        reasons=evaluate(before,after,limits,mode=str(self.config.get("trading.risk_mode","normal")))
        if reasons:raise FinancialError(reasons[0],403)
        return {**quote,"pool":pool,"position_before":position,"position_owner":owner,"operation":operation,"wire":wire,
            "minimum_liquidity":(request.get("parameters") or {}).get("minimum_liquidity",0),"inventory":inventory,
            "portfolio_effect":{"before":before.asdict(),"after":after.asdict(),"source":self.protocol+":lp_inventory","as_of":time.time()}}

    def validate(self,request,quote):
        pool,position,operation,wire,_,owner=self._plan(request)
        wire["deadline"]=quote["wire"]["deadline"]
        tx=v4_call(self.manager,self.sender,operation,pool["key"],wire) if self.v4 else v3_call(self.manager,self.sender,operation,wire,spacing=pool["tick_spacing"])
        if tx!=quote["transaction"] or owner!=quote["position_owner"]:raise FinancialError("lp_plan_mismatch",403)
        if position and position["liquidity"]!=quote["position_before"]["liquidity"]:raise FinancialError("lp_position_changed",403)
        super().validate(request,quote)

    def _cashflows(self,logs,pool,wire,adding):
        transfer="0x"+keccak(text="Transfer(address,address,uint256)").hex()
        receiver=self.deployment["contracts"]["pool_manager"] if self.v4 else pool["id"]
        values=[]
        for index,token in enumerate((pool["token0"],pool["token1"])):
            debit,credit=0,0
            for log in logs:
                topics=log.get("topics") or []
                if log.get("address","").lower()!=token.lower() or len(topics)!=3 or topics[0].lower()!=transfer:continue
                source,destination=topics[1][-40:].lower(),topics[2][-40:].lower()
                value=int(log["data"],16)
                if source==self.sender[2:].lower():
                    if destination!=receiver[2:].lower():raise FinancialError("lp_asset_debit_destination_mismatch",403)
                    debit+=value
                if destination==self.sender[2:].lower():
                    if source not in {receiver[2:].lower(),self.manager[2:].lower()}:raise FinancialError("lp_asset_credit_source_mismatch",403)
                    credit+=value
            if debit and not adding:raise FinancialError("lp_exit_spent_underlying_asset",403)
            if debit>wire[f"amount{index}_max"] and adding:raise FinancialError("lp_input_amount_bound_breached",403)
            values.append({"debit_base":str(debit),"credit_base":str(credit)})
        return values

    def status(self,request,quote,submission):
        result,receipt=self.verified_receipt(quote,submission)
        if receipt is None:return result
        try:
            operation=quote["operation"];wire=quote["wire"]
            token_id=nft_mint_id(receipt.get("logs") or [],self.manager,self.sender) if operation=="mint" else wire["token_id"]
            state=self._position(token_id,receipt["blockNumber"])
            before=int((quote["position_before"] or {}).get("liquidity",0));delta=int(state["liquidity"])-before
            flows=self._cashflows(receipt.get("logs") or [],quote["pool"],wire,operation in {"mint","increase"})
            if self.v4:
                proof=v4_receipt(receipt.get("logs") or [],self.deployment["contracts"]["pool_manager"],self.manager,tuple(quote["pool"]["key"]),
                    token_id,wire["tick_lower"],wire["tick_upper"],delta)
                if operation in {"mint","increase"} and delta!=wire["liquidity"]:raise FinancialError("lp_liquidity_mismatch",403)
                if operation=="decrease" and delta!=-wire["liquidity"]:raise FinancialError("lp_liquidity_mismatch",403)
                if operation=="collect" and delta:raise FinancialError("lp_collect_changed_liquidity",403)
                amounts={f"amount{index}_base":str(max(int(flow["debit_base"]),int(flow["credit_base"]))) for index,flow in enumerate(flows)}
            else:
                if operation=="collect":
                    if delta:raise FinancialError("lp_collect_changed_liquidity",403)
                    proof={"liquidity_delta":"0",**v3_collect_receipt(receipt.get("logs") or [],self.manager,self.sender,token_id,(wire["collect0_max"],wire["collect1_max"]))}
                else:
                    adding=operation in {"mint","increase"}
                    proof=v3_liquidity_receipt(receipt.get("logs") or [],self.manager,token_id,adding,
                        minimum_liquidity=quote["minimum_liquidity"] if adding else wire["liquidity"],exact_liquidity=None if adding else wire["liquidity"],
                        minima=(wire["amount0_min"],wire["amount1_min"]),maxima=(wire["amount0_max"],wire["amount1_max"]) if adding else None)
                    if int(proof["liquidity_delta"])!=delta:raise FinancialError("lp_position_receipt_mismatch",403)
                    if not adding:v3_collect_receipt(receipt.get("logs") or [],self.manager,self.sender,token_id,(wire["collect0_max"],wire["collect1_max"]))
                amounts=proof
            if operation in {"mint","increase"} and not any(int(flow["debit_base"]) for flow in flows) and not quote["position_before"]:
                raise FinancialError("lp_mint_asset_flow_evidence_missing",503)
            if not self.v4:
                for index,flow in enumerate(flows):
                    observed=int(flow["debit_base"] if operation in {"mint","increase"} else flow["credit_base"])
                    expected=int(amounts[f"amount{index}_base"])
                    if operation in {"mint","increase","collect"} and observed!=expected:
                        raise FinancialError("lp_asset_flow_event_mismatch",503)
            aid=submission.get("action_id")
            if not aid:raise FinancialError("protocol_action_identity_required",403)
            self.book.record(aid,key=self._key(token_id),wallet_id=request["wallet_id"],chain=self.chain,protocol=self.protocol,kind="lp",
                owner_address=self.sender,state=state,block_number=result["block_number"],block_hash=result["block_hash"],delta={"liquidity":str(delta)},
                proof={**proof,"transaction_hash":result["transaction_hash"],"block_hash":result["block_hash"]})
            value=sum((Decimal(amounts[f"amount{index}_base"])/10**quote["pool"]["decimals"][index]*Decimal(quote["pool"]["prices_usd"][index]) for index in (0,1)),Decimal(0))
            return {"state":"confirmed",**result,"position_id":str(token_id),"position_key":self._key(token_id),"protocol_effect":proof,
                    "cashflows":flows,"position":state,"settled_notional_usd":str(value)}
        except FinancialError as exc:return {"state":"needs_recovery",**result,"reason":exc.code}
