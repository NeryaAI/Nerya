"""Public records shared by SDK integrations and the task API."""
from typing import Any,Literal,TypedDict

RunOrigin=Literal['strategy_agent','scheduled_agent']
RunStage=Literal['admission','prepare','execute','approval','confirmation','delivery']


class RunResult(TypedDict,total=False):
    status:str
    final_text:str
    effects:list[dict[str,Any]]
    turn:dict[str,Any]


class TaskRun(TypedDict,total=False):
    run_id:str
    task_kind:RunOrigin
    task_id:str
    session_id:str
    command_id:str
    turn_id:str
    revision:int
    command_revision:int
    source_revision:str
    admission_status:str
    execution_status:str
    business_status:str
    delivery_status:str
    snapshot:dict[str,Any]
    result:RunResult
    created_at:float
    updated_at:float


class FinancialGrant(TypedDict,total=False):
    grant_id:str
    security_revision:str
    state:str
    policy:dict[str,Any]
    valid_from:float
    expires_at:float
    approved_by:str
    revision:int


class FinancialAction(TypedDict,total=False):
    action_id:str
    run_id:str
    kind:str
    quote_hash:str
    state:str
    request:dict[str,Any]
    submission:dict[str,Any]
    receipt:dict[str,Any]
    steps:list[dict[str,Any]]


class FinancialCapabilities(TypedDict,total=False):
    resource_id:str
    resource_type:Literal['account','wallet']
    provider:str
    kind:str
    supported:bool
    ready:bool
    reason:str
    live_verified:bool
