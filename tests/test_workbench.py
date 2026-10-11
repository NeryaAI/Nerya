from copy import deepcopy
import json
import threading
import time
import urllib.request
import pytest
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.agent.workbench import execution_view, session_view, filter_sessions
from nerya.agent.interactions import create_interaction, validate_payload
from nerya.agent.command_store import CommandError
from nerya.db.sqlite import connect


def config(tmp_path):
    return Config(paths=WorkspacePaths(root=tmp_path),data=deepcopy(DEFAULT_CONFIG))


def test_prose_and_validation_are_not_task_completion():
    assert execution_view([], [{"final_text":"all done"}])["execution"] == "idle"
    view=execution_view([], [{"stopped_reason":"end_turn","verifier_outcome":{"hard_status":"passed"}}])
    assert view["completion"] == "turn_finished"
    assert view["validation"] == "passed"
    external=execution_view([], [{"external_call":{"tool":"nerya_progress","arguments":{"status":"completed"},"status":"succeeded"}}],"tunnel")
    assert external["completion"] == "external_reported"
    assert external["external"]


def test_projection_retains_unknown_execution_and_search_escapes(tmp_path):
    from nerya.agent.command_runtime import CommandRuntime
    cfg=config(tmp_path); runtime=CommandRuntime(cfg,lambda *_:{},epoch="test")
    runtime.submit({"command_id":"command-test","session_id":"session-test","request":{"payload":{"text":"100% coverage"}}},start=False)
    runtime.store.claim("session-test","owner")
    con=connect(cfg.paths.db)
    con.execute("UPDATE agent_command_queues SET lease_until=0 WHERE session_id='session-test'")
    con.close()
    assert session_view(cfg,"session-test")["status"]["execution"]=="unconfirmed"
    assert not (cfg.paths.root/"state/agent_threads.sqlite3").exists()
    sessions=[{"session_id":"session-test","title":"100% coverage"}]
    assert len(filter_sessions(cfg,sessions,{"q":"%"}))==1
    assert filter_sessions(cfg,sessions,{"q":"not present"})==[]


def test_interaction_validation_and_idempotent_creation(tmp_path):
    cfg=config(tmp_path)
    first=create_interaction(cfg,sid="session-test",tid="turn-test",call_id="call-test",kind="question",payload={"title":"Which market?","choices":["A","B"]})
    assert first==create_interaction(cfg,sid="session-test",tid="turn-test",call_id="call-test",kind="question",payload={"title":"Which market?","choices":["A","B"]})
    with pytest.raises(CommandError):
        validate_payload("plan",{"title":"Plan","steps":[]})
    with pytest.raises(CommandError):
        validate_payload("question",{"title":"Market","choices":[{}]})


def test_plan_ceiling_applies_before_full_access():
    from nerya.tools.permissions import PermissionEngine,PermissionContext,PermissionMode,PermissionRequest
    from nerya.tools.registry import make_native_descriptor
    from nerya.tools.types import RiskLevel
    engine=PermissionEngine(); context=PermissionContext(mode=PermissionMode.YOLO,plan_only=True)
    descriptor=make_native_descriptor(name="write",description="write",input_schema={},handler=lambda c:None,risk=RiskLevel.WRITE,read_only=False)
    request=PermissionRequest(descriptor=descriptor,payload={})
    assert engine.evaluate(request,context).kind.value=="deny"


def test_user_wait_does_not_spend_execution_wall_budget():
    from nerya.agent.loop_state import LoopRunState,TurnCheckpoint
    from nerya.agent.loop_contracts import LoopConfig
    checkpoint=TurnCheckpoint(turn_id="turn-test",message_id="msg",deadline_epoch=110,
        control={"user_wait_started_at":100},terminal={"stop_reason":"user_input_pending"})
    state=LoopRunState.begin(config=LoopConfig(max_wall_seconds=60),user_message="",original_user_text="",now=1000,checkpoint=checkpoint,continuation_feedback="Answer")
    assert state.deadline_epoch==1010


def test_user_question_blocks_sibling_mutation_before_dispatch():
    from types import SimpleNamespace
    from nerya.agent.tool_phase import ToolBatchPhase,ToolBatchPolicy
    from nerya.agent.loop_state import LoopRunState
    from nerya.tools.registry import ToolRegistry,make_native_descriptor
    from nerya.tools.orchestrator import BatchResult
    from nerya.tools.types import ToolCall,ToolResult
    calls=[ToolCall(id="question",name="request_user_input"),ToolCall(id="write",name="write_file")]
    ran=[]
    def run(items):
        ran.extend(c.name for c in items)
        return BatchResult(results=[ToolResult.from_json(tool_use_id=c.id,name=c.name,data={"ok":True}) for c in items])
    registry=ToolRegistry()
    for name in ["request_user_input","write_file"]:
        registry.register(make_native_descriptor(name=name,description=name,input_schema={},handler=lambda c:None))
    result=ToolBatchPhase(orchestrator=SimpleNamespace(run_batch=run),registry=registry).run(calls,state=LoopRunState(turn_id="test",message_id="test"),policy=ToolBatchPolicy(frozenset(ran or ["request_user_input","write_file"]),frozenset(["request_user_input","write_file"])))
    assert ran==["request_user_input"]
    assert result.batch.results[1].is_error


def test_pending_interaction_rejects_direct_command_and_history_edit(tmp_path):
    from nerya.agent.command_runtime import CommandRuntime
    from nerya.agent.history_mutations import mutate_message,HistoryMutationError
    cfg=config(tmp_path);runtime=CommandRuntime(cfg,lambda *_:{},epoch="test")
    original={"command_id":"command-before-question","session_id":"session-test","request":{"payload":{"text":"Question"}}}
    runtime.submit(original,start=False)
    row=runtime.store.claim("session-test","owner")
    create_interaction(cfg,sid="session-test",tid=row["turn_id"],call_id="question",kind="question",payload={"title":"Continue?"})
    runtime.store.finish(row["command_id"],"session-test","owner","awaiting_input",{})
    with pytest.raises(CommandError,match="interaction_response_required"):
        runtime.submit({**original,"command_id":"command-bypass"},start=False)
    with pytest.raises(HistoryMutationError,match="history_busy"):
        mutate_message(cfg.paths,{"session_id":"session-test","message_id":row["turn_id"]+":user","content":"changed"})


@pytest.mark.parametrize("mode,tool,payload",[
    ("execute","request_user_input",{"title":"Select a market","questions":[{"id":"market","question":"Which market?","options":["BTC","ETH"]},{"id":"period","question":"Which period?"}]}),
    ("execute","request_user_input",{"title":"Strategy inputs","questions":[{"id":"market","question":"Which market?","options":["BTC","ETH"]},{"id":"period","question":"Which period?"}]}),
    ("plan","propose_plan",{"title":"Read and report","steps":["Read the evidence","Write the report"],"deliverables":["Report"]}),
])
@pytest.mark.parametrize("isolated", [False, True])
def test_real_http_kernel_question_and_plan_resume(tmp_path,monkeypatch,mode,tool,payload,isolated):
    from nerya.api.local_server import build_server
    from nerya.llm.messages import MessagesResponse
    import nerya.agent.kernel as kernel_module
    import nerya.mcp.openai_tunnel as tunnel
    monkeypatch.setattr(tunnel,"restore",lambda *a,**k:None)
    monkeypatch.setenv("NERYA_DASHBOARD_INTERNAL_TOKEN","isolated-workbench-test")
    calls=[]
    class Gateway:
        def __init__(self,*a,**k):pass
        def effective_model_metadata(self,*a,**k):return "fixture","test",{}
        def call_messages(self,**kwargs):
            calls.append(deepcopy(kwargs.get("messages",[])))
            if len(calls)==1:
                assert any(t.get("name")==tool for t in kwargs.get("tools",[])), [t.get("name") for t in kwargs.get("tools",[])]
                return MessagesResponse(content=[{"type":"tool_use","id":"interaction-call","name":tool,"input":payload}],stop_reason="tool_use",usage={"input_tokens":10,"output_tokens":10},provider="fixture",model="test")
            return MessagesResponse(content=[{"type":"text","text":"Task response after user decision."}],stop_reason="end_turn",usage={"input_tokens":10,"output_tokens":10},provider="fixture",model="test")
    monkeypatch.setattr(kernel_module,"LLMGateway",Gateway)
    cfg=config(tmp_path)
    server=build_server(cfg,host="127.0.0.1",port=0,start_continuous=False)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def http(path,body=None):
        req=urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}"+path,data=json.dumps(body).encode() if body is not None else None,headers={"content-type":"application/json","x-nerya-dashboard-internal":"isolated-workbench-test"})
        with urllib.request.urlopen(req,timeout=10) as response:return json.load(response)
    def settled(cid):
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            item=http("/agent/commands?session_id=session-test&command_id="+cid)["command"]
            if item["state"] not in ("queued","running","stopping"):return item
            time.sleep(.03)
        raise AssertionError("command did not settle")
    try:
        info=http("/runtime/info");assert "plan_mode" in info["capabilities"] and "goal_mode" in info["capabilities"]
        assert info["build_id"] and "root" not in info
        http("/agent/commands",{"command_id":"command-workbench","session_id":"session-test","request":{"payload":{"text":"Help with this task"},"work_mode":mode,"run_only":isolated}})
        first=settled("command-workbench")
        assert first["state"]=="awaiting_input", first
        view=http("/agent/sessions/view?session_id=session-test")
        assert view["status"]["waiting_for"]=="user"
        item=view["pending_interactions"][0]
        answer={"session_id":"session-test","interaction_id":item["interaction_id"],"expected_revision":item["revision"],"response_id":"response-request-test","selected":["BTC"] if mode=="execute" else [],"text":"Please continue"}
        if "questions" in payload:
            answer.update(selected=[], answers={"market":{"selected":["BTC"],"text":""},"period":{"selected":[],"text":"4h"}})
        receipt=http("/agent/interactions/respond",answer)
        final=settled(receipt["command"]["command_id"])
        assert final["state"]=="succeeded", final
        assert (final["turn_id"]==first["turn_id"]) == (mode=="execute")
        assert http("/agent/interactions/respond",answer)["duplicate"]
        # Recovering the same durable response after reload does not re-run tools.
        assert http("/agent/interactions/respond", {"action":"reconcile", "session_id":"session-test",
            "interaction_id":item["interaction_id"]})["duplicate"]
        assert len(calls)==2
        if "questions" in payload:
            resumed=json.dumps(calls[-1])
            assert "Which period?" in resumed and "4h" in resumed
        assert not http("/agent/sessions/view?session_id=session-test")["pending_interactions"]
    finally:
        server.shutdown();server.server_close();thread.join(3)


def test_structured_answer_validation():
    from nerya.agent.interactions import validate_answers
    payload=validate_payload("question", {"title":"Inputs", "questions":[
        {"id":"market","question":"Market?","options":["BTC","ETH"]},
        {"id":"rules","question":"Rules?","options":["ATR","Trend"],"multiple":True}]})
    validate_answers(payload, {})
    validate_answers(payload, {"market":{"text":"SOL"},"rules":{"selected":["ATR","Trend"],"text":"custom"}})
    for answer in [{"unknown":{"text":"BTC"}}, {"market":{"selected":["bad"]}}, {"market":{"selected":["BTC","ETH"]}}, {"market":{"selected":["BTC"],"text":"ETH"}}]:
        with pytest.raises(CommandError): validate_answers(payload, answer)
    with pytest.raises(CommandError):
        validate_payload("question", {"title":"Inputs","questions":[{"id":"a","question":"A"},{"id":"a","question":"B"}]})
