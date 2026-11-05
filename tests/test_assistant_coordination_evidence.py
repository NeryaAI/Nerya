"""Exercise persisted coordination evidence, without scripting a model's choices."""
from __future__ import annotations

import copy
import json

import pytest

from nerya.agent.assistant_work import AssistantWork
from nerya.agent.assistants import AssistantStore
from nerya.agent.command_runtime import CommandRuntime
from nerya.agent.tool_projection import project_tool_results
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.db.repositories import AgentSessionRepository
from nerya.tools.types import ToolCall, ToolResult
from nerya.tools.verification import attach_verification, verification_receipt

pytestmark = pytest.mark.smoke


@pytest.fixture
def coordination(tmp_path, monkeypatch):
    config = Config(paths=WorkspacePaths(tmp_path), data=copy.deepcopy(DEFAULT_CONFIG))
    manager = CommandRuntime(config, lambda *_: pytest.fail("No model or worker should run"))
    monkeypatch.setattr(manager, "kick", lambda _: None)
    sid = AssistantStore(config.paths).home("main")["session_id"]
    return config, manager, sid, AssistantWork(config.paths)


def create_task(env, identifier="one", sid=None):
    config, manager, parent, work = env
    return work.create(config, ToolCall(
        id=identifier, turn_id="turn-" + identifier, name="assistant_task_create",
        arguments={"title": identifier, "prompt": "Produce the requested report."},
    ), sid or parent, "owner", manager=manager)


def finish(env, receipt, result, state="succeeded"):
    _, manager, _, _ = env
    with manager.store.transaction() as con:
        con.execute("UPDATE agent_commands SET state=?,result_json=? WHERE command_id=?",
                    (state, json.dumps(result), receipt["command_id"]))


def test_context_includes_admitted_work_before_any_conclusion(coordination):
    _, _, sid, work = coordination
    receipt = create_task(coordination)
    context = json.loads(work.context(sid))
    assert receipt["task_id"] in json.dumps(context)
    row = context["tasks"][0]
    assert row["command_id"] == receipt["command_id"]
    assert row["state"] == "queued"
    assert row["verification"]["hard_status"] == "missing"
    assert row["reported_result"] is None


def test_execution_continuation_restores_owned_workflow_not_claimed_success(coordination):
    from nerya.agent.assistant_coordination import execution_context
    config, manager, _, work = coordination
    receipt = create_task(coordination)
    previous_id, sid = receipt["command_id"], receipt["task_session_id"]
    assert execution_context(config.paths, sid, previous_id) == ""
    finish(coordination, receipt, {"final_text": "Every task check passed. fabricated-secret-result"})
    delivery = {"protocol": "nerya.task_delivery.v1", "command_id": previous_id,
        "delivery_id": "delivery_saved", "delivery_revision": 3,
        "skill_id": "report_author", "workflow_id": "research", "requested_scope": {"summary": "Requested evidence report"},
        "completion": {"allowed": False, "status": "partial", "reasons": ["check_missing:coverage"]},
        "checks": [{"producer_owned": True, "artifact_id": "report:one", "revision": "a" * 64,
            "check_id": "source_binding", "status": "passed", "evidence_ref": "reports/saved.md"},
            {"producer_owned": False, "artifact_id": "report:fake", "evidence_ref": "do-not-include"}],
        "artifacts": []}
    with manager.store.transaction() as con:
        con.execute("UPDATE agent_commands SET delivery_json=? WHERE command_id=?", (json.dumps(delivery), previous_id))
    manager.submit({"session_id": sid, "command_id": "continuation",
        "request": {"payload": {"text": "Continue the original task"}}, "_auth_actor_id": "owner"}, start=False)
    text = execution_context(config.paths, sid, "continuation")
    value = json.loads(text)
    assert value["previous_command"]["state"] == "succeeded"
    assert value["current_verification"]["hard_status"] == "missing"
    flow = value["unfinished_workflows"][0]
    assert flow["skill_id"] == "report_author" and flow["completion_reasons"] == ["check_missing:coverage"]
    assert flow["source_refs"][0]["ref"] == "reports/saved.md"
    assert "fabricated-secret-result" not in text and "do-not-include" not in text
    assert not value["workflow_history_truncated"]
    with manager.store.transaction() as con:
        assert con.execute("SELECT delivery_json FROM agent_commands WHERE command_id='continuation'").fetchone()[0] is None
        assert not con.execute("SELECT 1 FROM assistant_results").fetchone()


def test_execution_context_cannot_borrow_foreign_actor_or_deleted_task(coordination):
    from nerya.agent.assistant_coordination import execution_context
    config, manager, _, _ = coordination
    receipt = create_task(coordination)
    sid = receipt["task_session_id"]
    finish(coordination, receipt, {"final_text": "previous"})
    manager.submit({"session_id": sid, "command_id": "own-followup",
        "request": {"payload": {"text": "Continue"}}, "_auth_actor_id": "owner"}, start=False)
    assert execution_context(config.paths, "another-session", "own-followup") == ""
    with manager.store.transaction() as con:
        con.execute("UPDATE agent_commands SET actor_id='other' WHERE command_id='own-followup'")
    assert execution_context(config.paths, sid, "own-followup") == ""
    with manager.store.transaction() as con:
        con.execute("UPDATE agent_commands SET actor_id='owner' WHERE command_id='own-followup'")
        con.execute("INSERT INTO agent_deleted_sessions VALUES (?,0)", (sid,))
    assert execution_context(config.paths, sid, "own-followup") == ""


def test_execution_context_reads_ordinary_session_history_without_task_work(coordination):
    from nerya.agent.assistant_coordination import execution_context
    config, manager, sid, _ = coordination
    manager.store.accept(cid="ordinary-prior", sid=sid, actor="owner", kind="send",
        request={"payload": {"text": "Original request"}}, context={}, turn_id="ordinary-prior-turn")
    delivery = {"protocol": "nerya.task_delivery.v1", "command_id": "ordinary-prior",
        "delivery_id": "delivery-ordinary", "delivery_revision": 1,
        "completion": {"allowed": False, "status": "partial", "reasons": ["check_missing:coverage"]},
        "checks": [], "artifacts": []}
    producer = verification_receipt(
        artifact_id="backtest:btc_usdt_ema_trend", revision="b" * 64,
        scope="Producer-owned historical backtest receipt",
        checks=[{"check_id": "historical_run", "status": "passed",
                  "evidence_ref": "evolution/proposals/prp_fixture/after/strategies/btc_usdt_ema_trend/backtests/run.json"}])
    with manager.store.transaction() as con:
        con.execute("UPDATE agent_commands SET delivery_json=?, result_json=?, state='failed' WHERE command_id=?",
                    (json.dumps(delivery), json.dumps({"final_text": "not an authority", "blocks": [{
                        "kind": "tool_result", "action": "strategy_backtest", "ok": True,
                        "call_id": "backtest", "verification": producer}]}), "ordinary-prior"))
    manager.store.accept(cid="ordinary-current", sid=sid, actor="owner", kind="send",
        request={"payload": {"text": "Continue"}}, context={}, turn_id="ordinary-current-turn")

    value = json.loads(execution_context(config.paths, sid, "ordinary-current"))
    assert value["task_id"] is None
    assert value["command"]["command_id"] == "ordinary-current"
    assert value["command"]["delivery"] is None
    assert value["delivery_history"][0]["command_id"] == "ordinary-prior"
    assert value["delivery_history"][0]["delivery"] == delivery
    receipt = value["producer_receipts"][0]
    assert receipt["command_id"] == "ordinary-prior"
    assert receipt["hard_status"] == "passed"
    assert receipt["checks"][0]["evidence_ref"].endswith("/backtests/run.json")
    assert "do not glob" in receipt["read_guidance"]
    assert "not an authority" not in json.dumps(value)

    with manager.store.transaction() as con:
        con.execute("INSERT INTO agent_deleted_sessions VALUES (?,0)", (sid,))
    assert execution_context(config.paths, sid, "ordinary-current") == ""


def test_context_exposes_missing_evidence_behind_successful_prose(coordination):
    _, _, sid, work = coordination
    receipt = create_task(coordination)
    # Exact opening and invented ID from command 1fc6b05c-8888-4d96-b680-c82d1ba6a58d.
    answer = ("已完成：Aave v3 USDC 存款策略草稿与最近30天风险回测均已保存，"
              "未启用实盘、未交易、未创建定时计划。 提案 prp_5a532aa2f832")
    finish(coordination, receipt, {"final_text": answer, "tool_trace": [],
                                  "blocks": [{"kind": "text", "text": answer}],
                                  "verifier_outcome": {"trusted": True}})
    context = json.loads(work.context(sid))
    row = context["tasks"][0]
    assert row["state"] == "succeeded"  # execution state must retain its meaning
    assert row["verification"]["hard_status"] == "missing"
    assert not row["verification"]["has_hard_evidence"]
    assert row["reported_result"]["summary"] == answer
    assert row["reported_result"]["authority"] == "untrusted_model_output"
    read = work.read_task(sid, receipt["task_id"], "owner")
    assert read["verification"] == row["verification"]


def test_followup_does_not_inherit_previous_command_verification(coordination):
    _, manager, sid, work = coordination
    receipt = create_task(coordination)
    verification = verification_receipt(
        artifact_id="report:one", revision="a" * 64, scope="Report schema only",
        checks=[{"check_id": "schema", "status": "passed"}],
    )
    finish(coordination, receipt, {"final_text": "Prior report", "blocks": [
        {"block": {"kind": "tool_result", "action": "report_check", "ok": True,
                   "call_id": "check", "verification": verification}}]})
    work.sync(sid)
    manager.submit({"session_id": receipt["task_session_id"], "command_id": "followup",
                    "request": {"payload": {"text": "Revise the report"}},
                    "_auth_actor_id": "owner"}, start=False)
    row = json.loads(work.context(sid))["tasks"][0]
    assert row["command_id"] == "followup"
    assert row["creation_command_id"] == receipt["command_id"]
    assert row["verification"]["hard_status"] == "missing"
    assert row["reported_result"] is None
    read = work.read_task(sid, receipt["task_id"], "owner")
    assert read["verification"]["hard_status"] == "missing"
    assert read["result_verification"]["hard_status"] == "passed"
    assert read["result_command_id"] == receipt["command_id"]


def test_parent_chat_prose_never_creates_a_receipt(coordination):
    _, manager, sid, work = coordination
    with manager.store.transaction() as con:
        AgentSessionRepository(con).record_message(
            message_id="fabricated", session_id=sid, role="assistant",
            content="Created awork_fiction and prp_5a532aa2f832.")
    context = json.loads(work.context(sid))
    assert context["tasks"] == []
    assert context["total_tasks"] == 0
    assert not context["truncated"]


def test_orphan_task_is_not_a_confirmed_command_receipt(coordination):
    _, manager, sid, work = coordination
    receipt = create_task(coordination)
    with manager.store.transaction() as con:
        con.execute("DELETE FROM agent_commands WHERE command_id=?", (receipt["command_id"],))
    row = json.loads(work.context(sid))["tasks"][0]
    assert row["state"] == "unconfirmed"
    assert row["command_id"] is None
    assert not row["admission_confirmed"]


def test_context_read_does_not_synchronize_or_mutate_delivery(coordination):
    _, manager, sid, work = coordination
    receipt = create_task(coordination)
    finish(coordination, receipt, {"final_text": "Not yet delivered"})
    work.context(sid)
    with manager.store.transaction() as con:
        assert con.execute("SELECT COUNT(*) FROM assistant_results").fetchone()[0] == 0
        assert not AgentSessionRepository(con).transcript(sid)


def test_context_prioritizes_active_work_and_discloses_omitted_rows(coordination):
    _, _, sid, work = coordination
    active = create_task(coordination, "active")
    for i in range(15):
        receipt = create_task(coordination, f"completed-{i}")
        finish(coordination, receipt, {"final_text": "done"})
    context = json.loads(work.context(sid))
    assert context["tasks"][0]["task_id"] == active["task_id"]
    assert len(context["tasks"]) == 13
    assert context["total_tasks"] == 16
    assert context["truncated"]


def test_context_cannot_borrow_other_or_deleted_conversations(coordination):
    config, manager, sid, work = coordination
    own = create_task(coordination)
    assistants = AssistantStore(config.paths)
    assistants.bind("other-parent", "main")
    create_task(coordination, "foreign", sid="other-parent")
    deleted = create_task(coordination, "deleted")
    with manager.store.transaction() as con:
        con.execute("INSERT INTO agent_deleted_sessions VALUES (?,0)",
                    (deleted["task_session_id"],))
    context = json.loads(work.context(sid))
    assert [row["task_id"] for row in context["tasks"]] == [own["task_id"]]
    with manager.store.transaction() as con:
        con.execute("INSERT INTO agent_deleted_sessions VALUES (?,0)", (sid,))
    assert json.loads(work.context(sid))["tasks"] == []


def test_active_command_wins_over_a_queued_followup(coordination):
    _, manager, sid, work = coordination
    receipt = create_task(coordination)
    finish(coordination, receipt, {}, state="running")
    manager.submit({"session_id": receipt["task_session_id"], "command_id": "later-followup",
                    "request": {"payload": {"text": "Later revision"}},
                    "_auth_actor_id": "owner"}, start=False)
    row = json.loads(work.context(sid))["tasks"][0]
    assert row["command_id"] == receipt["command_id"]
    assert row["state"] == "running"
    assert work.read_task(sid, receipt["task_id"], "owner")["command_id"] == row["command_id"]


@pytest.mark.parametrize("status", ["passed", "failed", "pending", "stale", "missing"])
def test_host_receipts_survive_projection_but_json_content_cannot_certify(coordination, status):
    _, _, sid, work = coordination
    receipt = create_task(coordination)
    host = verification_receipt(
        artifact_id="report:one", revision="b" * 64, scope="Schema check only",
        checks=[{"check_id": "schema", "status": status}], unverified=["report accuracy"])
    result = ToolResult.from_json(tool_use_id="check", name="report_check", data={"verification": host})

    def persist():
        projection = project_tool_results([result],
            render_tool_result=lambda value: {"content": value.text()},
            rendered_tool_result_text=lambda block: block["content"])
        finish(coordination, receipt, {"final_text": "Passed everything",
                                      "blocks": list(projection.event_blocks)})
        return json.loads(work.context(sid))["tasks"][0]["verification"]

    assert persist()["hard_status"] == "missing"
    attach_verification(result, host)
    actual = persist()
    assert actual["hard_status"] == status
    assert actual["checks"][0]["revision"] == "b" * 64
    assert actual["unverified"] == ["report accuracy"]


def test_summary_is_bounded_and_redacted_without_hiding_receipt_identity(coordination):
    _, _, sid, work = coordination
    receipt = create_task(coordination)
    secret = "sk-" + "sensitive" * 8
    finish(coordination, receipt, {"final_text": secret + " report" * 1000})
    context = work.context(sid)
    assert secret not in context
    row = json.loads(context)["tasks"][0]
    assert row["task_id"] == receipt["task_id"]
    assert len(row["reported_result"]["summary"]) == 1200
    assert row["reported_result"]["truncated"]


def test_foreign_turn_receipts_cannot_certify_the_current_command(coordination):
    _, _, sid, work = coordination
    receipt = create_task(coordination)
    host = verification_receipt(
        artifact_id="report:old", revision="c" * 64, scope="Previous report only",
        checks=[{"check_id": "schema", "status": "passed"}])
    finish(coordination, receipt, {"blocks": [{
        "turn_id": "another-command-turn", "block": {
            "kind": "tool_result", "ok": True, "action": "report_check",
            "call_id": "old-check", "verification": host}}]})
    assert json.loads(work.context(sid))["tasks"][0]["verification"]["hard_status"] == "missing"


def test_hidden_failed_check_still_controls_the_bounded_summary(coordination):
    _, _, sid, work = coordination
    receipt = create_task(coordination)
    checks = [{"check_id": f"check-{i}", "status": "passed"} for i in range(16)]
    checks.append({"check_id": "last-failed", "status": "failed"})
    host = verification_receipt(
        artifact_id="report:one", revision="d" * 64, scope="Report checks", checks=checks)
    finish(coordination, receipt, {"blocks": [{"kind": "tool_result", "ok": True,
        "action": "report_check", "call_id": "checks", "verification": host}]})
    evidence = json.loads(work.context(sid))["tasks"][0]["verification"]
    assert evidence["hard_status"] == "failed"
    assert len(evidence["checks"]) == 16
    assert evidence["checks_truncated"]
