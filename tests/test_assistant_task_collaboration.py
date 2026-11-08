import copy
import json
from types import SimpleNamespace

import pytest

from nerya.agent.assistant_work import AssistantWork
from nerya.agent.assistants import AssistantStore, AssistantError, assistant_config
from nerya.agent.command_runtime import CommandRuntime
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.tools.types import ToolCall
from nerya.tools.capability_policy import tool_policy_allows
from nerya.tools.native.tasks import assistant_task_collaboration_handler

pytestmark = pytest.mark.smoke

@pytest.fixture
def family(tmp_path,monkeypatch):
    config=Config(paths=WorkspacePaths(tmp_path),data=copy.deepcopy(DEFAULT_CONFIG))
    manager=CommandRuntime(config,lambda *_:{"final_text":"done"},epoch="family")
    monkeypatch.setattr(manager,"kick",lambda sid:None)
    monkeypatch.setattr("nerya.agent.command_runtime.runtime",lambda _:manager)
    store=AssistantStore(config.paths); parent=store.home("main")["session_id"]
    work=AssistantWork(config.paths)
    tasks=[work.create(config,ToolCall(id=name,turn_id="creation",name="assistant_task_create",arguments={"title":name,"prompt":"Read public evidence"}),parent,"owner",manager=manager) for name in ("first","second")]
    with manager.store.transaction() as con:
        for task in tasks:
            con.execute("UPDATE agent_commands SET state='succeeded',result_json=? WHERE command_id=?",(json.dumps({"final_text":"public result "+task["title"]}),task["command_id"]))
    work.sync()
    return SimpleNamespace(config=config,manager=manager,store=store,parent=parent,work=work,first=tasks[0],second=tasks[1])

def sender(f,sid=None,source="user_chat",mode="execute"):
    sid=sid or f.parent
    f.manager.store.accept(cid="sender-command",sid=sid,actor="owner",kind="send",turn_id="sender-turn",
        request={"source":source,"work_mode":mode,"payload":{"text":"Continue the task"}},context={})
    return f.manager.store.claim(sid,"sender-worker")

def send(f,sid=None,key="request-one",text="Review the evidence",mode="follow_up"):
    return f.work.message_task(f.config,sid or f.parent,f.second["task_id"],text,"owner",request_id=key,turn_id="sender-turn",mode=mode,manager=f.manager)

def test_parent_and_siblings_read_public_results_with_pagination(family):
    f=family
    assert len(f.work.visible_tasks(f.parent,"owner"))==2
    assert len(f.work.visible_tasks(f.first["task_session_id"],"owner"))==2
    row=f.work.read_task(f.first["task_session_id"],f.second["task_id"],"owner",limit=3)
    assert row["content"]=="pub" and row["next_offset"]==3
    assert f.work.read_task(f.parent,f.second["task_id"],"owner",offset=3)["content"]=="lic result second"
    with f.manager.store.transaction() as con:
        assert con.execute("SELECT COUNT(*) FROM assistant_task_access").fetchone()[0]==4

def test_other_assistants_actors_parents_and_deleted_tasks_are_denied(family):
    f=family
    other=f.store.home(f.store.save({"name":"Other"})["id"])["session_id"]
    separate=f.store.conversation("main")["session_id"]
    for sid,actor in [(other,"owner"),(separate,"owner"),(f.parent,"intruder"),(f.first["task_session_id"],"intruder")]:
        with pytest.raises(AssistantError,match="owner_mismatch"):
            f.work.read_task(sid,f.second["task_id"],actor)
    with f.manager.store.transaction() as con:
        con.execute("INSERT INTO agent_deleted_sessions VALUES (?,0)",(f.second["task_session_id"],))
    with pytest.raises(AssistantError,match="session_deleted"):
        f.work.read_task(f.parent,f.second["task_id"],"owner")
    assert len(f.work.visible_tasks(f.parent,"owner"))==1

def test_follow_up_is_atomic_idempotent_and_same_text_can_be_requested_again(family):
    f=family;sender(f)
    first=send(f);duplicate=send(f)
    assert duplicate["duplicate"] and duplicate["command_id"]==first["command_id"]
    with pytest.raises(AssistantError,match="conflict"):send(f,text="Changed")
    again=send(f,key="request-two")
    assert again["command_id"]!=first["command_id"]
    with f.manager.store.transaction() as con:
        assert con.execute("SELECT COUNT(*) FROM assistant_task_messages").fetchone()[0]==2
        req=json.loads(con.execute("SELECT request_json FROM agent_commands WHERE command_id=?",(first["command_id"],)).fetchone()[0])
        assert req["payload"]["task_message"]["sender_session_id"]==f.parent
        assert req["source"]=="assistant_task_message"


def test_bound_delivered_followup_cannot_relay_to_another_task(family):
    f = family
    sender(f, f.first["task_session_id"], source="assistant_task_message")
    with f.manager.store.transaction() as con:
        row = con.execute("SELECT request_json FROM agent_commands WHERE command_id='sender-command'").fetchone()
        request = json.loads(row[0])
        request["assistant_request_binding"] = {"command_id": "parent-instruction"}
        request["payload"]["task_message"] = {"sender_session_id": f.parent}
        con.execute("UPDATE agent_commands SET request_json=? WHERE command_id='sender-command'", (json.dumps(request),))
    with pytest.raises(AssistantError, match="already_delivered"):
        send(f, f.first["task_session_id"], text="Continue the task")
    with f.manager.store.transaction() as con:
        assert con.execute("SELECT COUNT(*) FROM assistant_task_messages").fetchone()[0] == 0
    # A distinct collaboration instruction remains available through the
    # existing parent-assignment rule; don't disable the whole capability.
    assert send(f, f.first["task_session_id"], text="Review the evidence")["ok"]

@pytest.mark.parametrize("state",["awaiting_input","awaiting_approval","unconfirmed"])
def test_waiting_and_unknown_execution_cannot_be_bypassed(family,state):
    f=family;sender(f)
    with f.manager.store.transaction() as con:
        con.execute("UPDATE agent_commands SET state=? WHERE command_id=?",(state,f.second["command_id"]))
    with pytest.raises(AssistantError):send(f)
    with f.manager.store.transaction() as con:
        assert con.execute("SELECT COUNT(*) FROM assistant_task_messages").fetchone()[0]==0

def test_paused_queue_and_plan_mode_are_preserved(family):
    f=family;sender(f,mode="plan")
    with pytest.raises(AssistantError,match="plan_only"):send(f)
    with f.manager.store.transaction() as con:
        con.execute("UPDATE agent_commands SET request_json=? WHERE command_id='sender-command'",(json.dumps({"payload":{"text":"continue"}}),))
        con.execute("UPDATE agent_command_queues SET paused=1,pause_reason='operator' WHERE session_id=?",(f.second["task_session_id"],))
    with pytest.raises(AssistantError,match="paused"):send(f)
    assert f.manager.store.snapshot(f.second["task_session_id"])["queue"]["paused"]

def test_sibling_message_uses_normal_approvals_and_cannot_guide_or_relay(family):
    f=family;sid=f.first["task_session_id"];sender(f,sid)
    with pytest.raises(AssistantError,match="peer_guide_denied"):send(f,sid,mode="guide")
    receipt=send(f,sid)
    with f.manager.store.transaction() as con:
        req=json.loads(con.execute("SELECT request_json FROM agent_commands WHERE command_id=?",(receipt["command_id"],)).fetchone()[0])
        assert req["permission_mode"]=="default"
        con.execute("UPDATE agent_commands SET request_json=? WHERE command_id='sender-command'",(json.dumps({"source":"assistant_task_message","payload":{"text":"relay"}}),))
    with pytest.raises(AssistantError,match="relay_blocked"):send(f,sid,key="relay-two")

def test_receipt_reopens_without_resending_and_result_identity_remains_explicit(family):
    f=family;sender(f);receipt=send(f)
    reopened=AssistantWork(f.config.paths)
    assert reopened.message_task(f.config,f.parent,f.second["task_id"],"Review the evidence","owner",request_id="request-one",turn_id="sender-turn",manager=f.manager)["duplicate"]
    current=reopened.read_task(f.parent,f.second["task_id"],"owner")
    assert current["command_id"]==receipt["command_id"]
    assert current["result_command_id"]==f.second["command_id"]
    assert current["state"]=="queued" and current["result_state"]=="succeeded"

def test_tools_use_trusted_session_identity_and_coordinator_can_discover_them(family):
    f=family
    policy=assistant_config(f.config,f.store.get("main"),session_id=f.parent).get("agent.native.tool_policy")
    for op in ("list","read","message"): assert tool_policy_allows(policy,"assistant_task_"+op)
    result=assistant_task_collaboration_handler(ToolCall(name="assistant_task_read",arguments={"task_id":f.second["task_id"]}),config=f.config,session_id=f.parent,actor_id="owner",operation="read")
    assert not result.is_error
    refused=assistant_task_collaboration_handler(ToolCall(name="assistant_task_list",metadata={"agent_id":"child"}),config=f.config,session_id=f.parent,actor_id="owner",operation="list")
    assert refused.is_error

def test_restart_recovers_queued_follow_up_but_not_manual_pause(family,monkeypatch):
    f=family;sender(f);send(f)
    restarted=CommandRuntime(f.config,lambda *_:{"final_text":"done"},epoch="new-process")
    started=[];monkeypatch.setattr(restarted,"kick",started.append)
    f.work.recover(restarted)
    assert f.second["task_session_id"] in started
    with restarted.store.transaction() as con:
        con.execute("UPDATE agent_command_queues SET paused=1,pause_reason='operator' WHERE session_id=?",(f.second["task_session_id"],))
    started.clear();f.work.recover(restarted)
    assert f.second["task_session_id"] not in started

def test_parent_guide_targets_the_running_turn_and_audit_matches(family):
    f=family;sender(f)
    send(f,key="run-target")
    active=f.manager.store.claim(f.second["task_session_id"],"target-worker")
    guide=send(f,key="guide-one",mode="guide")
    with f.manager.store.transaction() as con:
        row=con.execute("SELECT kind,turn_id FROM agent_commands WHERE command_id=?",(guide["command_id"],)).fetchone()
        assert row["kind"]=="guide" and row["turn_id"]==active["turn_id"]

def test_concurrent_duplicate_send_has_one_command_and_one_audit(family):
    from concurrent.futures import ThreadPoolExecutor
    f=family;sender(f)
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts=list(pool.map(lambda _:send(f),range(2)))
    assert len({r["command_id"] for r in receipts})==1
    assert sum(r["duplicate"] for r in receipts)==1
    with f.manager.store.transaction() as con:
        assert con.execute("SELECT COUNT(*) FROM assistant_task_messages").fetchone()[0]==1

def test_inactive_sender_cannot_start_another_task(family):
    with pytest.raises(AssistantError,match="sender_inactive"):send(family)

def test_task_can_follow_parent_assignment_but_peer_reply_cannot_ping_pong(family):
    f=family;sid=f.first["task_session_id"];sender(f,sid,source="assistant_task_message")
    with f.manager.store.transaction() as con:
        con.execute("UPDATE agent_commands SET request_json=? WHERE command_id='sender-command'",(json.dumps({"source":"assistant_task_message","payload":{"text":"ask peer","task_message":{"sender_session_id":f.parent}}}),))
    assert send(f,sid)["ok"]
    with f.manager.store.transaction() as con:
        con.execute("UPDATE agent_commands SET request_json=? WHERE command_id='sender-command'",(json.dumps({"source":"assistant_task_message","payload":{"text":"relay","task_message":{"sender_session_id":f.second["task_session_id"]}}}),))
    with pytest.raises(AssistantError,match="relay_blocked"):send(f,sid,key="loop-request")

def test_blocked_task_report_returns_to_parent_without_invented_success(family):
    f=family;sender(f);receipt=send(f)
    with f.manager.store.transaction() as con:
        con.execute("UPDATE agent_commands SET state='blocked',result_json=? WHERE command_id=?",(json.dumps({"final_text":"Need a source document"}),receipt["command_id"]))
    f.work.sync();f.work.sync()
    rows=f.work.results("main")
    row=next(r for r in rows if r["command_id"]==receipt["command_id"])
    assert row["state"]=="blocked" and row["summary"]=="Need a source document"
    with f.manager.store.transaction() as con:
        assert con.execute("SELECT COUNT(*) FROM agent_messages WHERE message_id=?",("assistant_result:"+receipt["command_id"],)).fetchone()[0]==1
