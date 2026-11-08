"""Bound follow-ups may run once without releasing a system-held queue."""

import hashlib
import json

import pytest

from nerya.agent.assistants import AssistantError
from nerya.agent.interactions import create_interaction
import test_assistant_receipts as assistant_receipts_fixtures
from test_assistant_receipts import create, followup, request

env = assistant_receipts_fixtures.env

pytestmark = pytest.mark.smoke
PROMPT = "继续刚才 ETH/USDT 策略任务，完成现有策略的30天回测和报告，不启用交易。"
PARENT_COMMAND = "26e07ed3-3723-4211-a45b-13c1e00b7a9e"


@pytest.fixture
def paused(env):
    cfg, contract = request(env, scopes=("read:funds",))
    task = create(env, cfg, contract)
    sid = task["task_session_id"]
    old = env.manager.store.claim(sid, "old-worker")
    assert env.manager.store.finish(old["command_id"], sid, "old-worker", "blocked",
                                    {"stopped_reason": "runtime_version_changed"})
    # Exercise the real restart normalization, entirely in this temp workspace.
    with env.manager.store.transaction() as con:
        con.execute("UPDATE agent_command_queues SET runtime_epoch='previous-process' WHERE session_id=?", (sid,))
        assert env.manager.store._queue(con, sid)["pause_reason"] == "restarted"
    cfg, contract = request(env, "task_follow_up", target=task["task_id"],
                            cid=PARENT_COMMAND, text=PROMPT, scopes=("read:funds",))
    return env, task, cfg, contract


def test_bound_followup_executes_only_new_send_and_keeps_pause_and_identity(paused):
    env, task, cfg, contract = paused
    sid = task["task_session_id"]
    before = env.manager.store.snapshot(sid, task["command_id"])["command"]
    sent = followup(env, cfg, contract, PROMPT)
    rid = "assistant_followup_" + PARENT_COMMAND
    digest = hashlib.sha256(json.dumps([env.sid, task["task_id"], rid]).encode()).hexdigest()[:32]
    assert sent["command_id"] == "atmsg_" + digest
    assert followup(env, cfg, contract, PROMPT)["duplicate"]
    with env.manager.store.transaction() as con:
        saved = dict(con.execute("SELECT * FROM agent_commands WHERE command_id=?", (sent["command_id"],)).fetchone())
        assert con.execute("SELECT message_id FROM assistant_task_messages").fetchone()[0] == rid + ":" + digest
    admitted = json.loads(saved["request_json"])
    assert admitted["run_only"] is True
    assert admitted["payload"]["text"] == PROMPT
    assert admitted["assistant_request_binding"] == contract["binding"]
    assert admitted["_auth_scopes"] == ["read:funds"]
    assert "resume_turn_id" not in admitted
    assert saved["kind"] == "send" and saved["turn_id"] != before["turn_id"]
    context = json.loads(saved["context_json"])
    assert context["run_once"] is True and context["run_once_epoch"] == env.manager.store.epoch
    # A later ordinary send must remain held even after this explicit one finishes.
    env.manager.store.accept(cid="later-command", sid=sid, actor="owner", kind="send",
        request={"payload": {"text": "Later work"}}, context={}, turn_id="later-turn")
    calls = []
    env.manager.execute = lambda _, payload: calls.append(payload) or {"final_text": "done", "stopped_reason": "end_turn"}
    claimed = env.manager.store.claim(sid, "followup-worker")
    assert claimed and claimed["command_id"] == sent["command_id"]
    env.manager._run(claimed, "followup-worker")
    assert len(calls) == 1 and calls[0]["payload"]["text"] == PROMPT
    assert env.manager.store.claim(sid, "next-worker") is None
    snapshot = env.manager.store.snapshot(sid)
    assert snapshot["queue"]["paused"] == 1 and snapshot["queue"]["pause_reason"] == "restarted"
    states = {row["command_id"]: row["state"] for row in snapshot["commands"]}
    assert states == {task["command_id"]: "blocked", sent["command_id"]: "succeeded", "later-command": "queued"}
    assert env.manager.store.snapshot(sid, task["command_id"])["command"] == before
    # Changed ambient authority and pause state cannot reissue or widen a retry.
    with env.manager.store.transaction() as con:
        source = json.loads(con.execute("SELECT request_json FROM agent_commands WHERE command_id=?", (PARENT_COMMAND,)).fetchone()[0])
        source["_auth_scopes"] = ["api:all"]
        con.execute("UPDATE agent_commands SET request_json=? WHERE command_id=?", (json.dumps(source), PARENT_COMMAND))
        con.execute("UPDATE agent_command_queues SET pause_reason='operator' WHERE session_id=?", (sid,))
    assert followup(env, cfg, contract, PROMPT)["duplicate"]
    with env.manager.store.transaction() as con:
        retry = con.execute("SELECT * FROM agent_commands WHERE command_id=?", (sent["command_id"],)).fetchone()
        assert (retry["request_json"], retry["fingerprint"]) == (saved["request_json"], saved["fingerprint"])
        assert con.execute("SELECT count(*) FROM assistant_task_messages").fetchone()[0] == 1


@pytest.mark.parametrize("barrier", [
    "operator", "unknown_reason", "interrupted", "worker", "queued", "running",
    "stopping", "delivering", "delivered", "unconfirmed", "awaiting_input",
    "awaiting_approval", "pending", "deferred", "answered", "unbound", "peer",
    "guide", "wrong_request_id", "changed_text",
])
def test_paused_followup_rejects_unsafe_or_unbound_requests_atomically(paused, barrier):
    env, task, cfg, contract = paused
    sid = task["task_session_id"]
    if barrier in {"pending", "deferred", "answered"}:
        create_interaction(env.config, sid=sid, tid="old-turn", call_id="question",
                           kind="question", payload={"title": "Confirm scope"})
        with env.manager.store.transaction() as con:
            con.execute("UPDATE agent_interactions SET state=? WHERE session_id=?", (barrier, sid))
    elif barrier in {"operator", "unknown_reason", "interrupted"}:
        with env.manager.store.transaction() as con:
            con.execute("UPDATE agent_command_queues SET pause_reason=? WHERE session_id=?", (barrier, sid))
    elif barrier == "worker":
        with env.manager.store.transaction() as con:
            con.execute("UPDATE agent_command_queues SET worker_owner='busy',lease_until=9999999999 WHERE session_id=?", (sid,))
    elif barrier == "unbound":
        cfg.data["runtime"].pop("assistant_request")
    elif barrier == "peer":
        peer = create(env, *request(env, cid="peer-create"))
        row = env.manager.store.claim(peer["task_session_id"], "peer-worker")
        assert env.manager.store.finish(row["command_id"], peer["task_session_id"], "peer-worker", "succeeded")
        cfg, contract = request(env, "task_follow_up", sid=peer["task_session_id"],
                                target=task["task_id"], cid="peer-followup", text=PROMPT)
    elif barrier not in {"guide", "wrong_request_id", "changed_text"}:
        with env.manager.store.transaction() as con:
            con.execute("UPDATE agent_commands SET state=? WHERE command_id=?", (barrier, task["command_id"]))
    binding = contract["binding"]
    before = env.manager.store.snapshot(sid)
    with pytest.raises(AssistantError):
        env.work.message_task(cfg, binding["session_id"], task["task_id"],
            PROMPT + " changed" if barrier == "changed_text" else PROMPT, "owner",
            request_id="wrong-request" if barrier == "wrong_request_id" else "assistant_followup_" + binding["command_id"],
            turn_id=binding["turn_id"], mode="guide" if barrier == "guide" else "follow_up", manager=env.manager)
    after = env.manager.store.snapshot(sid)
    assert after["queue"] == before["queue"]
    assert after["commands"] == before["commands"]
    with env.manager.store.transaction() as con:
        assert con.execute("SELECT count(*) FROM assistant_task_messages").fetchone()[0] == 0
