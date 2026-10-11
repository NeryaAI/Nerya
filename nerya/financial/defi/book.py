"""Protocol assets and strategy claims, distinct from observed wallet assets."""
import json
import time
from decimal import Decimal

from ...db.sqlite import connect_preview
from ..contracts import FinancialError
from ..store import FinancialStore, encode


def owner_id(context):
    if context.get("task_kind") in {"strategy_agent","strategy_script"}:
        if not context.get("task_id"):raise FinancialError("protocol_strategy_owner_required",403)
        return "strategy:"+context["task_id"]
    if not context.get("actor_id"):raise FinancialError("protocol_actor_required",403)
    return "actor:"+context["actor_id"]


class ProtocolBook:
    def __init__(self,config):self.config=config

    def claims(self,key,owner):
        con=connect_preview(self.config.paths.db)
        try:
            rows=con.execute("SELECT owner_id,units_json FROM protocol_position_claims WHERE position_key=?",(key,)).fetchall()
            selected={};total={}
            for row in rows:
                units=json.loads(row["units_json"])
                if row["owner_id"]==owner:selected=units
                for asset,value in units.items():total[asset]=total.get(asset,Decimal(0))+Decimal(value)
            return {k:Decimal(v) for k,v in selected.items()},total
        finally:con.close()

    def positions(self,*,chain,protocol,owner_address):
        con=connect_preview(self.config.paths.db)
        try:
            rows=con.execute("SELECT * FROM protocol_positions WHERE chain=? AND protocol=? AND LOWER(owner_address)=?",
                             (chain,protocol,owner_address.lower())).fetchall()
            return [{**dict(row),"state":json.loads(row["state_json"])} for row in rows]
        finally:con.close()

    def record(self,action_id,*,key,wallet_id,chain,protocol,kind,owner_address,state,block_number,block_hash,delta,proof):
        store=FinancialStore(self.config)
        with store.transaction() as con:
            action=con.execute("SELECT context_json FROM financial_actions WHERE action_id=?",(action_id,)).fetchone()
            if not action:raise FinancialError("protocol_action_identity_required",403)
            owner=owner_id(json.loads(action["context_json"]))
            old=con.execute("SELECT * FROM protocol_position_events WHERE action_id=?",(action_id,)).fetchone()
            if old:
                if old["position_key"]!=key or json.loads(old["delta_json"])!=delta:
                    raise FinancialError("protocol_receipt_identity_conflict",403)
                return False
            if proof.get("transaction_hash") is not None and proof.get("log_index") is not None:
                duplicate=con.execute("SELECT action_id FROM protocol_position_events WHERE json_extract(proof_json,'$.transaction_hash')=? "
                    "AND json_extract(proof_json,'$.log_index')=?",(proof["transaction_hash"],proof["log_index"])).fetchone()
                if duplicate:raise FinancialError("protocol_receipt_already_owned",403)
            claim=con.execute("SELECT units_json FROM protocol_position_claims WHERE position_key=? AND owner_id=?",(key,owner)).fetchone()
            units=json.loads(claim["units_json"]) if claim else {}
            for name,value in delta.items():
                changed=Decimal(units.get(name,"0"))+Decimal(value)
                if changed<0:raise FinancialError("protocol_exit_exceeds_owned_claim",403)
                units[name]=str(changed)
            now=time.time()
            con.execute("""INSERT INTO protocol_positions VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(position_key) DO UPDATE SET state_json=excluded.state_json,block_number=excluded.block_number,
                block_hash=excluded.block_hash,updated_at=excluded.updated_at WHERE excluded.block_number>=protocol_positions.block_number""",
                (key,wallet_id,chain,protocol,kind,owner_address,encode(state),block_number,block_hash,now))
            con.execute("""INSERT INTO protocol_position_claims VALUES (?,?,?,?)
                ON CONFLICT(position_key,owner_id) DO UPDATE SET units_json=excluded.units_json,updated_at=excluded.updated_at""",
                (key,owner,encode(units),now))
            con.execute("INSERT INTO protocol_position_events VALUES (?,?,?,?,?,?)",
                (action_id,key,owner,encode(delta),encode(proof),now))
            return True
