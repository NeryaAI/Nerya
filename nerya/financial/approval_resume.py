"""Resume only the financial action frozen in a durable human approval."""
import json
from .contracts import FinancialContext,FinancialError
from .gateway import FinancialGateway
from .store import FinancialStore


def resume_financial_approval(config,approval_id):
    store=FinancialStore(config)
    with store.transaction() as con:
        approval=con.execute("SELECT * FROM approvals WHERE id=?",(approval_id,)).fetchone()
        if not approval or approval["state"]!="approved":return {"ok":False,"error":"financial_approval_not_approved"}
        frozen=json.loads(approval["payload"])
        row=con.execute("SELECT * FROM financial_actions WHERE action_id=? AND approval_id=?",(frozen.get("financial_action_id"),approval_id)).fetchone()
        if not row or row["quote_hash"]!=frozen.get("quote_hash"):return {"ok":False,"error":"financial_approval_binding_mismatch"}
        data=json.loads(row["context_json"])
        data["scopes"]=frozenset(data.get("scopes",[]))
        context=FinancialContext(**data);aid=row["action_id"];quote_hash=row["quote_hash"]
    try:result={"ok":True,"action":FinancialGateway(config).execute(context,aid,quote_hash=quote_hash)}
    except FinancialError as exc:result={"ok":False,"error":exc.code,"action_id":aid}
    from ..agent.command_runtime import runtime
    runtime(config).financial_decision({**frozen,'approval_id':approval_id,'state':'approved'},result)
    return result
