from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

PROTOCOL_ACTIONS=frozenset({"lp_add","lp_remove","lp_collect","lp_rebalance","lend_supply","lend_withdraw","borrow","repay","redeem"})
ALLOWANCE_ACTIONS=frozenset({"contract_approval","permit2_approval"})
KINDS=frozenset({"trade","swap","exchange_transfer","withdraw","wallet_transfer","bridge_swap"})|PROTOCOL_ACTIONS|ALLOWANCE_ACTIONS
TRANSIENT=frozenset({"reserved","submitting","submitted","confirming","unconfirmed","needs_recovery"})


class FinancialError(ValueError):
    def __init__(self,code,status=409):
        self.code,self.status=code,status
        super().__init__(code)


def amount(value, *, zero=False):
    if isinstance(value,bool):raise FinancialError("invalid_amount",400)
    try:
        number=Decimal(str(value))
        if not number.is_finite() or number<0 or abs(number.adjusted())>255 or (not zero and number==0):raise InvalidOperation
    except (InvalidOperation,ValueError,TypeError):raise FinancialError("invalid_amount",400)
    return number


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()


def action_fingerprint(context,request,parent_action_id=None):
    value={"request":request,"run_id":context.run_id,"parent":parent_action_id}
    if context.strategy_run_id:value["strategy_run_id"]=context.strategy_run_id
    return digest(value)


def security_revision(value):
    def clean(item):
        if isinstance(item,dict):
            return {k:clean(v) for k,v in item.items() if k not in {"title","label","ui","position","created_at","updated_at"}}
        if isinstance(item,list):return [clean(v) for v in item]
        return item
    return digest(clean(value))


@dataclass(frozen=True)
class FinancialContext:
    actor_id: str
    scopes: frozenset[str]=field(default_factory=frozenset)
    run_id: str | None=None
    task_kind: str | None=None
    task_id: str | None=None
    security_revision: str | None=None
    command_id: str | None=None
    session_id: str | None=None
    turn_id: str | None=None
    plan_only: bool=False
    strategy_run_id: str | None=None

    def require(self,scope):
        if "api:all" not in self.scopes and scope not in self.scopes:
            raise FinancialError("financial_scope_denied",403)


def context_from_config(config, *, actor_id=None, scopes=()):
    script_run = config.get("runtime.strategy_financial_run_id")
    if script_run:
        from ..db.sqlite import connect
        con = connect(config.paths.db)
        try:
            row = con.execute("SELECT * FROM strategy_financial_runs WHERE run_id=?", (script_run,)).fetchone()
            if row is None:
                raise FinancialError("strategy_financial_run_not_found", 403)
            context = FinancialContext(row["actor_id"], frozenset(json.loads(row["scopes_json"])),
                                       task_kind="strategy_script", task_id=row["strategy_id"], security_revision=row["security_revision"],
                                       session_id=row["session_id"], plan_only=row["mode"] != "live", strategy_run_id=row["run_id"])
        finally:
            con.close()
        from .strategy_runtime import validate_context
        validate_context(config, context, preparing=True)
        return context
    rid=config.get("runtime.task_run_id")
    if not rid:
        if not actor_id:raise FinancialError("financial_actor_required",403)
        return FinancialContext(actor_id,frozenset(scopes),command_id=config.get('runtime.command_id'),
            session_id=config.get('runtime.command_session_id'),turn_id=config.get('runtime.command_turn_id'),
            plan_only=bool(config.get("agent.native.plan_only",False)))
    from ..db.sqlite import connect
    con=connect(config.paths.db)
    try:
        row=con.execute("SELECT * FROM agent_runs WHERE run_id=?",(rid,)).fetchone()
        cid=config.get("runtime.task_command_id")
        command=con.execute("""SELECT c.state FROM agent_commands c JOIN agent_run_commands l
            ON c.command_id=l.command_id WHERE l.run_id=? AND c.command_id=?""",(rid,cid)).fetchone()
        if not row or not command or command[0]!="running":raise FinancialError("financial_run_not_active",403)
        queue=con.execute("SELECT worker_owner,lease_until FROM agent_command_queues WHERE session_id=?",(row["session_id"],)).fetchone()
        if not queue or not queue[0] or queue[1]<=time.time():raise FinancialError("financial_run_lease_lost",403)
        snapshot=json.loads(row["snapshot_json"])
        return FinancialContext(row["actor_id"],frozenset(scopes),rid,row["task_kind"],row["task_id"],
            snapshot.get("security_revision") or security_revision(snapshot.get("definition",snapshot)),cid,
            session_id=row['session_id'],turn_id=config.get('runtime.command_turn_id'),plan_only=bool(config.get("agent.native.plan_only",False)))
    finally:con.close()


def normalize_request(raw):
    if not isinstance(raw,dict) or raw.get("kind") not in KINDS:raise FinancialError("unsupported_financial_action",400)
    allowed={"kind","account_id","wallet_id","asset","amount","market","chain","to_chain","to_asset",
             "recipient","memo","from_account","to_account","spender","slippage_bps","max_fee_usd","plan",
             "component_id","protocol","position_id","parameters"}
    if set(raw)-allowed:raise FinancialError("unknown_financial_parameter",400)
    request=json.loads(json.dumps(raw))
    if "component_id" in request:
        import re
        if not isinstance(request["component_id"],str) or not re.fullmatch(r"(?:builtin:[a-z][a-z0-9_-]{0,63}|user:[a-z][a-z0-9_-]{0,63}:[a-z][a-z0-9_-]{0,63})",request["component_id"]):
            raise FinancialError("invalid_financial_component_id",400)
    if "parameters" in request and (not isinstance(request["parameters"],dict) or len(json.dumps(request["parameters"]))>65536):
        raise FinancialError("invalid_component_parameters",400)
    if not request.get("account_id") and not request.get("wallet_id"):raise FinancialError("financial_resource_required",400)
    if request["kind"] in PROTOCOL_ACTIONS:
        if not request.get("chain") or not request.get("wallet_id") or not request.get("protocol"):
            raise FinancialError("protocol_resource_required",400)
        if "amount" in request:request["amount"]=format(amount(request["amount"],zero=request["kind"] in {"lp_collect","redeem"}),"f")
    elif request["kind"]!="trade":
        request["amount"]=format(amount(request.get("amount"),zero=request["kind"] in ALLOWANCE_ACTIONS),"f")
        if not request.get("asset"):raise FinancialError("asset_required",400)
    if request["kind"] in {"withdraw","wallet_transfer","bridge_swap"} and not request.get("recipient"):
        raise FinancialError("recipient_required",400)
    if request["kind"] in ALLOWANCE_ACTIONS and not request.get("spender"):raise FinancialError("spender_required",400)
    if request["kind"]=="permit2_approval" and (not request.get("wallet_id") or not request.get("chain") or not request.get("protocol")):
        raise FinancialError("protocol_resource_required",400)
    if request["kind"]=="exchange_transfer" and (not request.get("from_account") or not request.get("to_account")):
        raise FinancialError("account_types_required",400)
    if request["kind"]=="bridge_swap" and (not request.get("to_chain") or not request.get("to_asset")):
        raise FinancialError("bridge_destination_required",400)
    if request["kind"] in {"swap","bridge_swap"}:
        slippage=request.get("slippage_bps",50)
        if type(slippage) is not int or not 0<=slippage<=1000:raise FinancialError("invalid_slippage",400)
        request["slippage_bps"]=slippage
    return request


def normalize_policy(raw):
    policy=json.loads(json.dumps(raw))
    if not isinstance(policy.get("actions"),list) or not policy["actions"] or set(policy["actions"])-KINDS:
        raise FinancialError("invalid_grant_actions",400)
    resources=policy.get("resources")
    if not isinstance(resources,dict) or not (resources.get("accounts") or resources.get("wallets")):
        raise FinancialError("grant_resources_required",400)
    for key,values in resources.items():
        if key not in {"accounts","wallets","assets","markets","chains","recipients","spenders","account_types","routers",'memos',"components","protocols","positions"}:
            raise FinancialError("unknown_grant_resource",400)
        if not isinstance(values,list) or any(not isinstance(v,str) or not v or "*" in v for v in values):
            raise FinancialError("explicit_grant_resources_required",400)
    limits=policy.get("limits")
    if not isinstance(limits,dict):raise FinancialError("finite_grant_limits_required",400)
    for key in ("single_usd","rolling_24h_usd","total_usd","fee_usd"):
        limits[key]=format(amount(limits.get(key),zero=key=="fee_usd"),"f")
    limits['rolling_24h_fee_usd']=format(amount(limits.get('rolling_24h_fee_usd',limits['fee_usd']),zero=True),'f')
    if amount(limits["single_usd"])>min(amount(limits["rolling_24h_usd"]),amount(limits["total_usd"])):
        raise FinancialError("inconsistent_grant_limits",400)
    if type(limits.get("slippage_bps",0)) is not int or not 0<=limits.get("slippage_bps",0)<=1000:
        raise FinancialError("invalid_grant_slippage",400)
    quantities=limits.get("asset_amounts",{})
    if not isinstance(quantities,dict):raise FinancialError("invalid_asset_limits",400)
    if set(policy["actions"])-{"trade"} and not quantities:raise FinancialError("asset_quantity_limits_required",400)
    limits["asset_amounts"]={key:format(amount(value),"f") for key,value in quantities.items()}
    if set(policy["actions"]) & PROTOCOL_ACTIONS and (not resources.get("components") or not resources.get("protocols") or not resources.get("chains")):
        raise FinancialError("grant_protocol_components_required",400)
    if 'trade' in policy['actions']:
        if not resources.get('markets'):raise FinancialError('grant_markets_required',400)
        choices=limits.get('trade_actions')
        if not isinstance(choices,list) or not choices or set(choices)-{'open_position','close_position','reduce_position','attach_protection'}:
            raise FinancialError('grant_trade_actions_required',400)
        limits['max_leverage']=format(amount(limits.get('max_leverage')),'f')
    for kind,key in (("withdraw","recipients"),("wallet_transfer","recipients"),("bridge_swap","recipients"),("contract_approval","spenders"),("permit2_approval","spenders")):
        if kind in policy["actions"] and not resources.get(key):raise FinancialError("grant_"+key+"_required",400)
    return policy
