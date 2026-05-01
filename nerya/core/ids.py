"""ID helpers. Separate functions so we can inspect and validate kinds."""

from __future__ import annotations

import uuid


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def event_id() -> str: return new_id("evt")
def intent_id() -> str: return new_id("int")
def order_id() -> str: return new_id("ord")
def fill_id() -> str: return new_id("fil")
def session_id() -> str: return new_id("ses")
def proposal_id() -> str: return new_id("prp")
def approval_id() -> str: return new_id("apv")
def skill_call_id() -> str: return new_id("skc")
def message_id() -> str: return new_id("msg")
def review_id() -> str: return new_id("rvw")
def script_run_id() -> str: return new_id("srn")
def turn_id() -> str: return new_id("trn")
def turn_step_id() -> str: return new_id("stp")

# Trading control-plane (04-29).
def reservation_id() -> str: return new_id("rsv")
def executor_id() -> str: return new_id("exc")
def position_id() -> str: return new_id("pos")
def protection_id() -> str: return new_id("prt")
def snapshot_id() -> str: return new_id("snp")
def risk_evaluation_id() -> str: return new_id("rsk")
def reconcile_id() -> str: return new_id("rec")
def trade_plan_id() -> str: return new_id("tpl")
def promotion_id() -> str: return new_id("pmo")
def evidence_id() -> str: return new_id("evd")
