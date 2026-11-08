"""Publication receipts use isolated durable producer records, never model claims."""

from copy import deepcopy
import json
import socket
from types import SimpleNamespace

import pytest

from nerya.agent.assistant_intent import assistant_request_digest
from nerya.agent.assistant_receipts import (
    AssistantReceiptError,
    render_assistant_receipt,
    verify_assistant_receipt,
)
from nerya.agent.assistant_work import AssistantWork
from nerya.agent.assistants import AssistantError, AssistantStore
from nerya.agent.command_runtime import CommandRuntime
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.task_delivery import start_delivery, update_delivery
from nerya.tools.types import ToolCall

pytestmark = pytest.mark.smoke


@pytest.fixture
def env(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("No worker, network or model execution is allowed")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    config = Config(WorkspacePaths(tmp_path), deepcopy(DEFAULT_CONFIG))
    manager = CommandRuntime(config, forbidden)
    monkeypatch.setattr(manager, "kick", lambda sid: None)
    store = AssistantStore(config.paths)
    sid = store.home("main")["session_id"]
    return SimpleNamespace(
        config=config,
        manager=manager,
        store=store,
        sid=sid,
        work=AssistantWork(config.paths),
    )


def request(
    env,
    operation="task_create",
    *,
    target=None,
    cid="parent-command",
    actor="owner",
    sid=None,
    language="en",
    scopes=("read:config",),
    text="Produce the requested report",
):
    sid = sid or env.sid
    with env.manager.store.transaction() as con:
        con.execute(
            "UPDATE agent_commands SET state='succeeded' WHERE session_id=? AND state='running'",
            (sid,),
        )
        con.execute(
            "UPDATE agent_command_queues SET worker_owner='',lease_until=0 WHERE session_id=?",
            (sid,),
        )
    accepted = env.manager.submit(
        {
            "command_id": cid,
            "session_id": sid,
            "_auth_actor_id": actor,
            "_auth_scopes": list(scopes),
            "request": {
                "payload": {
                    "text": text,
                    "attachments": [
                        {"id": "input-one", "name": "Brief", "reference": "brief.md"}
                    ],
                }
            },
        },
        start=False,
    )
    row = env.manager.store.claim(sid, "request-worker")
    assert row and row["command_id"] == accepted["command"]["command_id"]
    contract = {
        "protocol": "nerya.assistant_request.v1",
        "disposition": operation,
        "status": "ready",
        "title": "Report",
        "target_task_id": target,
        "language": language,
        "clarification": "",
        "origin": "explicit",
        "binding": {
            "command_id": cid,
            "session_id": sid,
            "turn_id": row["turn_id"],
            "actor_id": actor,
            "input_digest": assistant_request_digest(json.loads(row["request_json"])),
        },
    }
    with env.manager.store.transaction() as con:
        context = json.loads(row["context_json"])
        context["assistant_request"] = contract
        con.execute(
            "UPDATE agent_commands SET context_json=? WHERE command_id=?",
            (json.dumps(context), cid),
        )
    cfg = Config(env.config.paths, deepcopy(env.config.data))
    cfg.data["runtime"].update(
        command_id=cid,
        command_session_id=sid,
        command_turn_id=row["turn_id"],
        financial_actor_id=actor,
        assistant_request=deepcopy(contract),
    )
    return cfg, contract


def create(env, cfg, contract, **arguments):
    binding = contract["binding"]
    return env.work.create(
        cfg,
        ToolCall(
            id="assistant_request_" + binding["command_id"],
            name="assistant_task_create",
            turn_id=binding["turn_id"],
            arguments={
                "title": contract["title"],
                "prompt": "Produce the requested report",
                **arguments,
            },
        ),
        binding["session_id"],
        binding["actor_id"],
        manager=env.manager,
    )


def followup(env, cfg, contract, text="Produce the requested report"):
    b = contract["binding"]
    return env.work.message_task(
        cfg,
        b["session_id"],
        contract["target_task_id"],
        text,
        b["actor_id"],
        request_id="assistant_followup_" + b["command_id"],
        turn_id=b["turn_id"],
        manager=env.manager,
    )


def verify(cfg, contract, receipt):
    return verify_assistant_receipt(cfg, contract, receipt, contract["disposition"])


def finish(env, receipt, **fields):
    with env.manager.store.transaction() as con:
        con.execute(
            "UPDATE agent_commands SET state='succeeded',result_json=? WHERE command_id=?",
            (json.dumps(fields), receipt["command_id"]),
        )


def test_create_keeps_frontend_contract_and_ignores_claimed_state(env):
    cfg, contract = request(env)
    receipt = create(env, cfg, contract, assistant_request_binding={"actor_id": "evil"})
    verified = verify(
        cfg,
        contract,
        {**receipt, "ok": False, "state": "succeeded", "task_complete": True},
    )
    assert verified["protocol"] == "nerya.assistant_task.v2" and verified["ok"]
    assert verified["publication_protocol"] == contract["protocol"]
    assert verified["state"] == "queued" and not verified["task_complete"]
    assert verified["reported_result"] is None
    with env.manager.store.transaction() as con:
        work = json.loads(
            con.execute("SELECT request_json FROM assistant_work").fetchone()[0]
        )
        admitted = json.loads(
            con.execute(
                "SELECT request_json FROM agent_commands WHERE command_id=?",
                (receipt["command_id"],),
            ).fetchone()[0]
        )
    assert work["assistant_request_binding"] == contract["binding"]
    assert admitted["payload"]["assistant_request_binding"] == contract["binding"]
    assert admitted["_auth_scopes"] == ["read:config"]
    assert receipt["task_session_id"] in render_assistant_receipt(contract, verified)


@pytest.mark.parametrize("field", ["task_id", "task_session_id", "command_id"])
def test_counterfeit_identifiers_fail_even_with_valid_task(env, field):
    cfg, contract = request(env)
    receipt = create(env, cfg, contract)
    with pytest.raises(AssistantReceiptError, match="identity_mismatch"):
        verify(cfg, contract, {**receipt, field: "counterfeit", "ok": True})


def test_old_copied_and_orphan_receipts_fail_closed(env):
    cfg, first = request(env)
    old = create(env, cfg, first)
    cfg, current = request(env, cid="new-request")
    with pytest.raises(AssistantReceiptError):
        verify(cfg, current, old)
    new = create(env, cfg, current)
    with pytest.raises(AssistantReceiptError, match="identity_mismatch"):
        verify(cfg, current, old)
    with env.manager.store.transaction() as con:
        con.execute(
            "DELETE FROM agent_commands WHERE command_id=?", (new["command_id"],)
        )
    with pytest.raises(AssistantReceiptError, match="admission_missing"):
        verify(cfg, current, new)


@pytest.mark.parametrize(
    "field", ["command_id", "session_id", "turn_id", "actor_id", "input_digest"]
)
def test_binding_contract_and_runtime_tampering_fail(env, field):
    cfg, contract = request(env)
    receipt = create(env, cfg, contract)
    forged = deepcopy(contract)
    forged["binding"][field] = "forged"
    with pytest.raises(AssistantReceiptError):
        verify(cfg, forged, receipt)
    cfg.data["runtime"]["assistant_request"] = forged
    with pytest.raises(AssistantReceiptError):
        verify(cfg, forged, receipt)


@pytest.mark.parametrize(
    "field", ["text", "attachments", "work_mode", "assistant_intent"]
)
def test_input_digest_is_checked_against_durable_request(env, field):
    cfg, contract = request(env)
    receipt = create(env, cfg, contract)
    with env.manager.store.transaction() as con:
        raw = json.loads(
            con.execute(
                "SELECT request_json FROM agent_commands WHERE command_id='parent-command'"
            ).fetchone()[0]
        )
        if field == "text":
            raw["payload"][field] = "Different task"
        elif field == "attachments":
            raw["payload"][field] = []
        else:
            raw[field] = "changed"
        con.execute(
            "UPDATE agent_commands SET request_json=? WHERE command_id='parent-command'",
            (json.dumps(raw),),
        )
    with pytest.raises(AssistantReceiptError, match="input_mismatch"):
        verify(cfg, contract, receipt)


@pytest.mark.parametrize(
    "mutation",
    [
        "work_actor",
        "work_parent",
        "child_actor",
        "child_session",
        "work_binding",
        "child_binding",
        "deleted",
    ],
)
def test_cross_actor_foreign_and_rebound_children_fail(env, mutation):
    cfg, contract = request(env)
    receipt = create(env, cfg, contract)
    with env.manager.store.transaction() as con:
        if mutation == "work_actor":
            con.execute("UPDATE assistant_work SET actor_id='intruder'")
        elif mutation == "work_parent":
            con.execute("UPDATE assistant_work SET parent_session_id='foreign'")
        elif mutation == "child_actor":
            con.execute(
                "UPDATE agent_commands SET actor_id='intruder' WHERE command_id=?",
                (receipt["command_id"],),
            )
        elif mutation == "child_session":
            con.execute(
                "UPDATE agent_commands SET session_id=? WHERE command_id=?",
                (env.sid, receipt["command_id"]),
            )
        elif mutation == "work_binding":
            con.execute("UPDATE assistant_work SET request_json='{}'")
        elif mutation == "child_binding":
            con.execute(
                "UPDATE agent_commands SET request_json='{}' WHERE command_id=?",
                (receipt["command_id"],),
            )
        else:
            con.execute(
                "INSERT INTO agent_deleted_sessions VALUES (?,0)",
                (receipt["task_session_id"],),
            )
    with pytest.raises(AssistantReceiptError):
        verify(cfg, contract, receipt)


@pytest.mark.parametrize(
    "state", ["queued", "stopping", "succeeded", "failed", "unconfirmed"]
)
def test_source_must_still_be_running_before_publication(env, state):
    cfg, contract = request(env)
    receipt = create(env, cfg, contract)
    with env.manager.store.transaction() as con:
        con.execute(
            "UPDATE agent_commands SET state=? WHERE command_id='parent-command'",
            (state,),
        )
    with pytest.raises(AssistantReceiptError, match="inactive"):
        verify(cfg, contract, receipt)


def test_create_idempotency_and_recovery_keep_frozen_binding(env):
    cfg, contract = request(env)
    receipt = create(env, cfg, contract)
    assert create(env, cfg, contract)["command_id"] == receipt["command_id"]
    with env.manager.store.transaction() as con:
        assert con.execute("SELECT COUNT(*) FROM assistant_work").fetchone()[0] == 1
        con.execute(
            "DELETE FROM agent_commands WHERE command_id=?", (receipt["command_id"],)
        )
    AssistantWork(cfg.paths).recover(env.manager)
    assert verify(cfg, contract, receipt)["admission_confirmed"]
    assert create(env, cfg, contract)["command_id"] == receipt["command_id"]


def test_followup_admission_is_bound_and_idempotent(env):
    cfg, contract = request(env)
    task = create(env, cfg, contract)
    cfg, contract = request(
        env, "task_follow_up", target=task["task_id"], cid="followup-command"
    )
    sent = followup(env, cfg, contract)
    assert followup(env, cfg, contract)["duplicate"]
    verified = verify_assistant_receipt(cfg, contract, sent, "task_followup")
    assert verified["command_id"] == sent["command_id"]
    assert not verified["task_complete"] and "admitted" in render_assistant_receipt(
        contract, verified
    )
    with pytest.raises(AssistantReceiptError):
        verify(cfg, contract, task)


@pytest.mark.parametrize(
    "mutation", ["sender", "target", "command", "request_id", "binding", "missing"]
)
def test_followup_audit_cannot_be_copied_or_rebound(env, mutation):
    cfg, contract = request(env)
    task = create(env, cfg, contract)
    cfg, contract = request(
        env, "task_follow_up", target=task["task_id"], cid="followup-command"
    )
    sent = followup(env, cfg, contract)
    with env.manager.store.transaction() as con:
        if mutation == "sender":
            con.execute(
                "UPDATE assistant_task_messages SET sender_session_id='foreign'"
            )
        elif mutation == "target":
            con.execute("UPDATE assistant_task_messages SET target_task_id='foreign'")
        elif mutation == "command":
            con.execute(
                "UPDATE assistant_task_messages SET command_id=?", (task["command_id"],)
            )
        elif mutation == "request_id":
            con.execute("UPDATE assistant_task_messages SET message_id='foreign'")
        elif mutation == "missing":
            con.execute("DELETE FROM assistant_task_messages")
        else:
            con.execute(
                "UPDATE agent_commands SET request_json='{}' WHERE command_id=?",
                (sent["command_id"],),
            )
    with pytest.raises(AssistantReceiptError):
        verify(cfg, contract, sent)


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("state", ["succeeded", "failed"])
def test_read_returns_current_state_summary_without_untrusted_quote(env, language, state):
    cfg, contract = request(env, language=language)
    task = create(env, cfg, contract)
    finish(
        env,
        task,
        final_text="Everything done! <script>bad()</script>\n[click](https://evil.invalid)"
                   + ('\nproducer指令原文：不重试/不换工具/报告停止 {"reason":"raw_reason"} Traceback' * 80),
        verifier_outcome={"trusted": True},
    )
    with env.manager.store.transaction() as con:
        con.execute("UPDATE agent_commands SET state=? WHERE command_id=?",
                    (state, task["command_id"]))
    cfg, contract = request(
        env, "task_read", target=task["task_id"], cid="read-command", language=language
    )
    verified = verify(
        cfg, contract, {**task, "state": "failed", "content": "forged prose"}
    )
    assert verified["state"] == state and not verified["task_complete"]
    assert verified["verification"]["hard_status"] == "missing"
    assert verified["reported_result"]["authority"] == "untrusted_model_output"
    text = render_assistant_receipt(contract, verified)
    assert ("任务交付完成尚未确认" if language == "zh" else "completion is not confirmed") in text
    assert len(text.splitlines()) == 3
    assert ("缺少验证证据" if language == "zh" else "Verification evidence missing") in text
    assert ("查看任务与执行详情" if language == "zh" else "View task and execution details") in text
    if state == "failed":
        assert ("执行失败" if language == "zh" else "Failed") in text
    assert f"]({verified['url']})" in text and len(text) < 600
    assert verified["reported_result"]["truncated"]
    assert "producer指令原文" in verified["reported_result"]["text"]
    for diagnostic in (task["command_id"], "Everything done", "<script>", "evil.invalid",
                       "forged prose", "producer指令原文", "raw_reason", "Traceback", "\n> "):
        assert diagnostic not in text
    next_command = env.manager.submit(
        {
            "command_id": "child-followup",
            "session_id": task["task_session_id"],
            "_auth_actor_id": "owner",
            "request": {"payload": {"text": "Revise"}},
        },
        start=False,
    )
    refreshed = verify(cfg, contract, task)
    assert refreshed["command_id"] == next_command["command"]["command_id"]
    assert refreshed["state"] == "queued" and refreshed["reported_result"] is None


@pytest.mark.parametrize("language", ["zh", "en"])
def test_only_read_of_authoritative_delivery_can_claim_completion(env, language):
    import hashlib
    from nerya.tools.verification import verification_receipt

    cfg, contract = request(env, language=language)
    task = create(env, cfg, contract)
    finish(env, task, final_text="Public answer")
    report = cfg.paths.root / "report.txt"
    report.write_text("Public answer", encoding="utf-8")
    content = report.read_bytes()
    revision = hashlib.sha256(content).hexdigest()
    artifact_id = "report:verified"
    # Host fixture for the generic delivery evaluator, not a model-supplied
    # contract or a native readback tool. The check actually reads the artifact.
    producer = verification_receipt(artifact_id=artifact_id, revision=revision,
        scope="Persisted report readback only",
        checks=[{"check_id": "report_readback",
                 "status": "passed" if content == b"Public answer" else "failed",
                 "evidence_ref": "report.txt"}])
    delivery = start_delivery(
        cfg.paths,
        command_id=task["command_id"],
        session_id=task["task_session_id"],
        skill_id="tasks",
        workflow_id="report",
        requested_scope={"summary": "Report"},
        stages=[{"id": "report", "required_checks": ["report_readback"],
                 "required_artifacts": [artifact_id]}],
        workflow_contract={"skill_id": "tasks", "workflow_id": "report",
            "source": "test:report_producer", "revision": revision,
            "required_checks": ["report_readback"], "required_artifacts": [artifact_id]},
    )
    assert not verify(cfg, contract, task)["task_complete"]
    completed = update_delivery(
        cfg.paths,
        command_id=task["command_id"],
        session_id=task["task_session_id"],
        expected_revision=delivery["delivery_revision"],
        stages=[{"id": "report", "state": "completed"}],
        artifacts=[{"artifact_id": artifact_id, "ref": "report.txt", "revision": revision}],
        producer_receipt=producer,
    )
    assert completed["completion"]["allowed"] is True
    assert completed["completion"]["status"] == "complete"
    cfg, contract = request(
        env, "task_read", target=task["task_id"], cid="read-command", language=language
    )
    verified = verify(cfg, contract, task)
    assert verified["task_complete"]
    assert (
        "任务交付已完成" if language == "zh" else "Task delivery complete"
    ) in render_assistant_receipt(contract, verified)


def test_list_derives_owned_current_tasks_and_ignores_model_list(env):
    cfg, contract = request(env)
    first = create(env, cfg, contract)
    cfg, contract = request(env, cid="second-command")
    second = create(env, cfg, contract)
    cfg, contract = request(env, cid="foreign-command", actor="intruder")
    foreign = create(env, cfg, contract)
    cfg, contract = request(env, "task_read", cid="list-command")
    verified = verify(
        cfg, contract, [foreign, {"task_id": "fake", "state": "succeeded"}]
    )
    assert {row["task_id"] for row in verified["tasks"]} == {
        first["task_id"],
        second["task_id"],
    }
    assert verified["total_tasks"] == 2
    assert all(row["state"] == "queued" for row in verified["tasks"])
    text = render_assistant_receipt(contract, verified)
    assert first["task_session_id"] in text and foreign["task_session_id"] not in text


def test_verification_is_query_only_and_does_not_repair_missing_records(
    env, monkeypatch
):
    from nerya.agent import assistant_receipts

    cfg, contract = request(env)
    task = create(env, cfg, contract)
    cfg, contract = request(
        env, "task_read", target=task["task_id"], cid="read-command"
    )
    readonly = assistant_receipts.connect_readonly
    connections = []

    def connect(path):
        con = readonly(path)
        assert con.execute("PRAGMA query_only").fetchone()[0] == 1
        connections.append(path)
        return con

    monkeypatch.setattr(assistant_receipts, "connect_readonly", connect)

    def snapshot():
        with env.manager.store.transaction() as con:
            return list(con.iterdump())

    before = snapshot()
    verify(cfg, contract, task)
    assert snapshot() == before and connections == [cfg.paths.db]


def test_unavailable_storage_and_incomplete_contract_fail_structurally(env, tmp_path):
    cfg, contract = request(env)
    missing = Config(WorkspacePaths(tmp_path / "missing"), deepcopy(cfg.data))
    with pytest.raises(AssistantReceiptError) as err:
        verify(missing, contract, {})
    assert err.value.code == "assistant_receipt_unavailable" and err.value.status == 503
    assert not missing.paths.db.exists()
    with pytest.raises(AssistantReceiptError):
        verify(cfg, {**contract, "status": "clarify"}, {})
    with pytest.raises(AssistantReceiptError):
        render_assistant_receipt(contract, {"ok": True, "task_complete": True})


@pytest.mark.parametrize(
    "actor", ["admin:password", "local:loopback", "local:anonymous"]
)
def test_legacy_default_actor_remains_readable_and_followable_by_owner_alias(
    env, actor
):
    legacy = env.work.create(
        env.config,
        ToolCall(
            id="legacy",
            name="assistant_task_create",
            turn_id="legacy-turn",
            arguments={"title": "Legacy", "prompt": "Public report"},
        ),
        env.sid,
        "default",
        manager=env.manager,
    )
    cfg, contract = request(env, "task_read", target=legacy["task_id"], actor=actor)
    assert verify(cfg, contract, legacy)["task_id"] == legacy["task_id"]
    cfg, contract = request(env, "task_read", cid="list-command", actor=actor)
    assert verify(cfg, contract, [legacy])["tasks"][0]["task_id"] == legacy["task_id"]
    cfg, contract = request(
        env,
        "task_follow_up",
        target=legacy["task_id"],
        cid="followup-command",
        actor=actor,
    )
    sent = followup(env, cfg, contract)
    assert verify(cfg, contract, sent)["admission_confirmed"]
    with env.manager.store.transaction() as con:
        assert (
            con.execute(
                "SELECT actor_id FROM agent_commands WHERE command_id=?",
                (sent["command_id"],),
            ).fetchone()[0]
            == "default"
        )


@pytest.mark.parametrize(
    "ceiling,source,expected",
    [
        (["api:all"], ["read:funds"], ["read:funds"]),
        (["read:funds"], ["api:all"], ["read:funds"]),
        (["api:all"], [], []),
        ([], ["api:all"], []),
        (["api:all"], ["api:all"], ["api:all"]),
        (["read:funds", "trade:paper"], ["read:funds", "read:config"], ["read:funds"]),
    ],
)
def test_followup_scope_intersection_is_frozen_on_retry(env, ceiling, source, expected):
    cfg, contract = request(env, scopes=ceiling)
    task = create(env, cfg, contract)
    cfg, contract = request(
        env,
        "task_follow_up",
        target=task["task_id"],
        cid="followup-command",
        scopes=source,
    )
    sent = followup(env, cfg, contract)
    with env.manager.store.transaction() as con:
        admitted = json.loads(
            con.execute(
                "SELECT request_json FROM agent_commands WHERE command_id=?",
                (sent["command_id"],),
            ).fetchone()[0]
        )
        assert admitted["_auth_scopes"] == expected
        raw = json.loads(
            con.execute(
                "SELECT request_json FROM agent_commands WHERE command_id='followup-command'"
            ).fetchone()[0]
        )
        raw["_auth_scopes"] = ["api:all"]
        con.execute(
            "UPDATE agent_commands SET request_json=? WHERE command_id='followup-command'",
            (json.dumps(raw),),
        )
    assert followup(env, cfg, contract)["duplicate"]
    with env.manager.store.transaction() as con:
        assert (
            json.loads(
                con.execute(
                    "SELECT request_json FROM agent_commands WHERE command_id=?",
                    (sent["command_id"],),
                ).fetchone()[0]
            )["_auth_scopes"]
            == expected
        )


def test_bound_producers_preserve_exact_text_and_reject_rewrites(env):
    text = "  Inspect evidence exactly.  \n"
    cfg, contract = request(env, text=text)
    with pytest.raises(AssistantError, match="input_mismatch"):
        create(env, cfg, contract, prompt=text.strip())
    task = create(env, cfg, contract, prompt=text)
    with env.manager.store.transaction() as con:
        assert (
            json.loads(
                con.execute(
                    "SELECT request_json FROM agent_commands WHERE command_id=?",
                    (task["command_id"],),
                ).fetchone()[0]
            )["payload"]["text"]
            == text
        )
    cfg, contract = request(
        env, "task_follow_up", target=task["task_id"], cid="followup-command", text=text
    )
    with pytest.raises(AssistantError, match="input_mismatch"):
        followup(env, cfg, contract, text.strip())
    sent = followup(env, cfg, contract, text)
    assert verify(cfg, contract, sent)["admission_confirmed"]
    with env.manager.store.transaction() as con:
        assert (
            con.execute("SELECT message FROM assistant_task_messages").fetchone()[0]
            == text
        )
