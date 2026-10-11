"""Trading gates validate a producer-owned, already reserved financial action."""
import json
import time
from .contracts import FinancialError,amount
from ..db.sqlite import connect


def check_trade_permit(config,*,account_id,market,notional_usd=None):
    aid=config.get("runtime.financial_action_id")
    if not aid:return None
    con=connect(config.paths.db)
    try:
        row=con.execute("SELECT * FROM financial_actions WHERE action_id=?",(aid,)).fetchone()
        if not row or row["state"] not in {"submitting","submitted"} or row["kind"]!="trade":raise FinancialError("invalid_trading_financial_permit",403)
        request=json.loads(row["request_json"]);quote=json.loads(row["quote_json"])
        if request.get("account_id")!=account_id or request.get("market")!=market:raise FinancialError("trading_financial_resource_mismatch",403)
        if row["grant_id"]:
            grant=con.execute("SELECT * FROM financial_grants WHERE grant_id=?",(row["grant_id"],)).fetchone()
            if not grant or grant["state"]!="active" or grant["expires_at"]<=time.time():raise FinancialError("financial_grant_revoked_or_expired")
        else:
            approval=con.execute("SELECT * FROM approvals WHERE id=?",(row["approval_id"],)).fetchone()
            if not approval or approval["state"]!="approved" or approval["expires_at"]<=time.time():raise FinancialError("financial_approval_not_valid",403)
        if notional_usd is not None and amount(notional_usd,zero=True)>amount(quote["risk_usd"],zero=True):raise FinancialError("resolved_trade_exceeds_financial_authorization",403)
        return row["action_id"]
    finally:con.close()
