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
    from nerya.agent.assistant_coordination import execution_context, task_command_projection
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
        current = con.execute("SELECT * FROM agent_commands WHERE command_id='ordinary-current'").fetchone()
        public = task_command_projection(con, {"task_session_id": sid, "actor_id": "owner"}, current)
        assert public["delivery_history"][0]["delivery"] == delivery
        con.execute("INSERT INTO agent_deleted_sessions VALUES (?,0)", (sid,))
    assert execution_context(config.paths, sid, "ordinary-current") == ""


def test_failed_tool_recovery_survives_toolless_followups_as_historical_advice(coordination):
    from nerya.agent.assistant_coordination import execution_context, task_command_projection
    from nerya.tools.types import ToolError, ToolErrorKind

    config, manager, sid, _ = coordination
    diagnostics = {"provider": "fixture_provider", "action": "fetch_history",
        "http_status": 403, "error_code": "ACCESS_DENIED",
        "error_category": "configuration", "error_name": "PermissionError",
        "owner_action_required": True}
    recovery = {"action": "inspect_configuration", "safe_to_retry": False,
        "requires_approval": False, "diagnostics": {**diagnostics,
            "body": "PRIVATE_BODY", "source_url": "https://private.example",
            "args": {"token": "PRIVATE_ARGS"}, "path": "/private/source"},
        "reason": "PRIVATE_REASON", "path": "/private/hint", "arbitrary_hint": True}
    error = ToolResult.from_error(tool_use_id="failed-call", name="fixture_tool",
        error=ToolError(kind=ToolErrorKind.PROVIDER_ERROR, message="PRIVATE_ERROR",
                        retryable=False, recovery_hint=recovery))
    projection = project_tool_results([error],
        render_tool_result=lambda result: {"content": result.text()},
        rendered_tool_result_text=lambda block: block["content"])
    failed_block = projection.event_blocks[0]
    assert failed_block["ok"] is False and failed_block["recovery"] == recovery

    def persist(cid, blocks, *, actor="owner", session=sid):
        turn_id = cid + "-turn"
        manager.store.accept(cid=cid, sid=session, actor=actor, kind="send",
            request={"payload": {"text": "Continue"}}, context={}, turn_id=turn_id)
        assert manager.store.claim(session, "offline-worker")["command_id"] == cid
        assert manager.store.finish(cid, session, "offline-worker", "succeeded", result={
            "final_text": "PRIVATE_PROSE " + json.dumps(recovery), "blocks": blocks})
        return turn_id

    origin_turn = persist("recovery-origin", [
        {"turn_id": "recovery-origin-turn", "block": failed_block}])
    # Neither another actor nor another session can lend recovery facts.
    persist("foreign-actor", [failed_block], actor="other")
    persist("foreign-session", [failed_block], session="another-session")
    # These invalid/non-recovery records must not consume the history limit.
    for index in range(24):
        persist(f"recovery-toolless-{index}", [
            {"kind": "text", "text": json.dumps(recovery), "recovery": recovery},
            {"turn_id": "foreign-turn", "block": failed_block},
            {**failed_block, "ok": True},
            {**failed_block, "recovery": None},
            {**failed_block, "recovery": {"action": "read /private/hint"}},
            {**failed_block, "recovery": {"action": ""}},
            {**failed_block, "recovery": {}},
            {"kind": "tool_result", "ok": False, "result": {"recovery": recovery}}])
    scope_hint = {"action": "inspect_scope_extraction", "safe_to_retry": False,
                  "diagnostics": {"provider": "fixture_provider", "action": "extract_scope",
                      "error_name": "/private/error", "http_status": [403]}}
    scope_error = ToolResult.from_error(tool_use_id="scope-call", name="unrelated_tool",
        error=ToolError(kind=ToolErrorKind.EXECUTION_ERROR, recovery_hint=scope_hint))
    scope_projection = project_tool_results([scope_error],
        render_tool_result=lambda result: {"content": result.text()},
        rendered_tool_result_text=lambda block: block["content"])
    scope_turn = persist("recovery-scope", list(scope_projection.event_blocks))
    manager.store.accept(cid="recovery-current", sid=sid, actor="owner", kind="send",
        request={"payload": {"text": "Read historical facts"}}, context={}, turn_id="current-turn")
    with manager.store.transaction() as con:
        before = [tuple(row) for row in con.execute(
            "SELECT command_id,state,result_json,delivery_json FROM agent_commands ORDER BY command_id")]
        current = con.execute("SELECT * FROM agent_commands WHERE command_id='recovery-current'").fetchone()
        public = task_command_projection(con, {"task_session_id": sid, "actor_id": "owner"}, current)
    context_text = execution_context(config.paths, sid, "recovery-current")
    context = json.loads(context_text)
    historical = context["failed_tool_recoveries"]
    assert [(item["command_id"], item["turn_id"], item["call_id"]) for item in historical] == [
        ("recovery-scope", scope_turn, "scope-call"), ("recovery-origin", origin_turn, "failed-call")]
    assert [item["action"] for item in historical] == ["unrelated_tool", "fixture_tool"]
    assert historical[0]["recovery"] == {"action": "inspect_scope_extraction", "safe_to_retry": False,
        "diagnostics": {"provider": "fixture_provider", "action": "extract_scope"}}
    assert historical[1]["recovery"] == {"action": "inspect_configuration", "safe_to_retry": False,
        "requires_approval": False, "diagnostics": diagnostics}
    assert all("Historical advisory" in item["scope"] and "current preconditions unknown" in item["scope"]
               for item in historical)
    assert not context["failed_tool_recoveries_truncated"]
    assert not context["current_verification"]["has_hard_evidence"]
    assert context["current_verification"]["checks"] == [] and context["producer_receipts"] == []
    assert context["previous_command"]["state"] == "succeeded"
    assert "PRIVATE" not in context_text and "/private" not in context_text
    assert "https://" not in context_text and "arbitrary_hint" not in context_text
    assert "failed_tool_recoveries" not in public
    with manager.store.transaction() as con:
        assert [tuple(row) for row in con.execute(
            "SELECT command_id,state,result_json,delivery_json FROM agent_commands ORDER BY command_id")] == before
        assert task_command_projection(con, {"task_session_id": sid, "actor_id": "owner"}, current) == public


def test_continuation_observations_read_hash_bound_outputs_not_assistant_prose(coordination):
    import hashlib
    from nerya.agent.assistant_coordination import execution_context, task_command_projection

    config, manager, _, _ = coordination
    receipt = create_task(coordination, "coverage-record")
    cid, sid = receipt["command_id"], receipt["task_session_id"]
    folder = config.paths.root / "backtests/one"
    folder.mkdir(parents=True)
    run = {"status": "completed", "strategy_id": "fixture", "proposal_id": "prp_fixture"}
    metrics = {"provenance": {"source_revision": "a" * 64, "source_changed_during_run": False,
                  "assumptions": {"max_gap_seconds": 90000}},
        "requested_start_utc": "2025-02-01T00:00:00Z", "requested_end_utc": "2025-02-04T00:00:00Z",
        "coverage_ok": False, "requested_window_complete": False, "performance_evidence": False,
        "modeled_coverage": {"covered_seconds": 165596.898, "requested_seconds": 259200, "complete": False},
        "gaps": [{"start": "2025-02-01T01:00:00Z", "end": "2025-02-02T03:00:03.102Z", "seconds": 93603.102}],
        "observation_coverage": {"leading_gap_seconds": 100, "trailing_gap_seconds": 50},
        "provider_body": "PRIVATE not in context"}
    (folder / "run.json").write_text(json.dumps(run))
    (folder / "report.md").write_text("Producer report")
    path = folder / "metrics.json"
    path.write_text(json.dumps(metrics))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    rel = path.relative_to(config.paths.root).as_posix()
    proof = verification_receipt(artifact_id="strategy:fixture:proposal:prp_fixture", revision="a" * 64,
        scope="Historical recorded producer checks", checks=[
            {"check_id": "request_scope_match", "status": "passed",
             "evidence_ref": f"request_scope:{cid}:{'b' * 64}:{digest}:{rel}"},
            {"check_id": "historical_run", "status": "passed", "evidence_ref": str(folder.relative_to(config.paths.root) / "run.json")},
            {"check_id": "report_output", "status": "passed", "evidence_ref": str(folder.relative_to(config.paths.root) / "report.md")},
            {"check_id": "data_coverage", "status": "failed"}])
    result = attach_verification(ToolResult.from_json(tool_use_id="replay", name="strategy_backtest", data={"ok": True}), proof)
    projected = project_tool_results([result], render_tool_result=lambda r: {"content": []}, rendered_tool_result_text=lambda _: None)
    finish(coordination, receipt, {"final_text": "Everything complete. PRIVATE invented prose",
                                  "blocks": [{"block": block} for block in projected.event_blocks]})
    narrative = "CONFLICTING_MODEL_PROSE: boundary offsets explain all excluded coverage"
    delivery = {"protocol": "nerya.task_delivery.v1", "command_id": cid,
        "delivery_id": "delivery-coverage", "delivery_revision": 3,
        "skill_id": "backtest", "workflow_id": "historical_replay",
        "requested_scope": {"summary": "Original pool and window; no trading",
            "scope_hash": "b" * 64, "authority": "admitted_request",
            "limitations_are_authorization": False,
            "limitations": [narrative], "model_interpretation": narrative},
        "completion": {"allowed": False, "status": "incomplete",
            "reasons": ["stage_blocked", "check_missing:data_coverage"]},
        "stages": [{"id": "replay", "state": "blocked", "label": narrative,
            "reason": narrative, "required_checks": ["data_coverage"],
            "required_artifacts": ["report"]}],
        "artifacts": [{"artifact_id": "report", "kind": "backtest_report",
            "ref": "backtests/one/report.md", "revision": "a" * 64,
            "status": "recorded", "label": narrative}],
        "checks": [{"artifact_id": proof["artifact_id"], "revision": "a" * 64,
            "check_id": "data_coverage", "status": "failed", "required": True,
            "producer_owned": True, "label": narrative, "message": narrative},
            {"check_id": "model_claim", "status": "missing", "producer_owned": False,
             "label": narrative}],
        "limitations": [narrative],
        "recovery": {"action": "wait_for_reconciliation", "safe_to_retry": False,
            "requires_receipt_reconciliation": True, "execution_unknown": True,
            "requires_approval": True, "financial_execution_allowed": False, "reason": narrative}}
    with manager.store.transaction() as con:
        con.execute("UPDATE agent_commands SET delivery_json=? WHERE command_id=?",
                    (json.dumps(delivery), cid))
    # More than the old 17-command window: prose/quoted JSON is not a host receipt.
    for index in range(24):
        followup = f"tool-less-followup-{index}"
        manager.store.accept(cid=followup, sid=sid, actor="owner", kind="send",
            request={"payload": {"text": "Continue"}}, context={}, turn_id=followup)
        finish(coordination, {"command_id": followup}, {
            "final_text": json.dumps(proof), "verification": proof,
            "blocks": [{"kind": "text", "text": json.dumps(proof), "verification": proof},
                       {"kind": "tool_result", "action": "quoted_receipt", "ok": True,
                        "result": {"verification": proof}},
                       {"turn_id": "foreign-turn", "block": {"kind": "tool_result",
                        "action": "foreign_receipt", "ok": True, "verification": proof}}]})
    manager.store.accept(cid="coverage-followup", sid=sid, actor="owner", kind="send",
        request={"payload": {"text": "Why incomplete?"}}, context={}, turn_id="coverage-followup-turn")
    current_delivery = {**delivery, "command_id": "coverage-followup", "delivery_id": "delivery-current"}
    with manager.store.transaction() as con:
        con.execute("UPDATE agent_commands SET delivery_json=? WHERE command_id=?",
                    (json.dumps(current_delivery), "coverage-followup"))
        before = tuple(con.execute("SELECT result_json,delivery_json FROM agent_commands WHERE command_id=?",
                                   (cid,)).fetchone())
    context = json.loads(execution_context(config.paths, sid, "coverage-followup"))
    assert [item["command_id"] for item in context["producer_receipts"]] == [cid]
    saved = context["producer_receipts"][0]
    observation = saved["observations"][0]
    assert observation["metrics_sha256"] == digest
    assert observation["modeled_coverage"] == metrics["modeled_coverage"]
    assert observation["excluded_gaps"] == metrics["gaps"]
    assert observation["max_gap_seconds"] == 90000
    assert observation["raw_acquisition"] == metrics["observation_coverage"]
    assert observation["performance_evidence"] is False
    assert "not current verification" in observation["scope"]
    assert saved["hard_status"] == "failed" and not context["current_verification"]["has_hard_evidence"]
    assert "PRIVATE" not in json.dumps(context)
    assert "CONFLICTING_MODEL_PROSE" not in json.dumps(context)
    for record, source in ((context["command"], current_delivery),
                           (context["delivery_history"][0], delivery)):
        reduced = record["delivery"]
        assert reduced["command_id"] == source["command_id"]
        assert reduced["delivery_id"] == source["delivery_id"]
        assert reduced["delivery_revision"] == 3
        assert (reduced["skill_id"], reduced["workflow_id"]) == ("backtest", "historical_replay")
        assert reduced["completion"] == delivery["completion"]
        assert reduced["requested_scope"] == {key: delivery["requested_scope"][key] for key in
            ("summary", "scope_hash", "authority", "limitations_are_authorization")}
        assert reduced["recovery"] == {key: value for key, value in delivery["recovery"].items()
                                       if key != "reason"}
        assert reduced["stages"] == [{key: value for key, value in delivery["stages"][0].items()
                                      if key not in {"label", "reason"}}]
        assert reduced["artifacts"] == [{key: value for key, value in delivery["artifacts"][0].items()
                                         if key != "label"}]
        assert reduced["checks"] == [{key: value for key, value in delivery["checks"][0].items()
                                      if key not in {"label", "message"}}]
    with manager.store.transaction() as con:
        assert tuple(con.execute("SELECT result_json,delivery_json FROM agent_commands WHERE command_id=?",
                                 (cid,)).fetchone()) == before
        current = con.execute("SELECT * FROM agent_commands WHERE command_id='coverage-followup'").fetchone()
        work = con.execute("SELECT * FROM assistant_work WHERE task_session_id=?", (sid,)).fetchone()
        public = task_command_projection(con, work, current)
        assert public["command"]["delivery"] == current_delivery
        assert public["delivery_history"][0]["delivery"] == delivery

    legacy_scope = {key: value for key, value in delivery["requested_scope"].items() if key != "authority"}
    with manager.store.transaction() as con:
        for source in (delivery, current_delivery):
            con.execute("UPDATE agent_commands SET delivery_json=? WHERE command_id=?",
                        (json.dumps({**source, "requested_scope": legacy_scope}), source["command_id"]))
    legacy = json.loads(execution_context(config.paths, sid, "coverage-followup"))
    for record in (legacy["command"], legacy["delivery_history"][0]):
        scope = record["delivery"]["requested_scope"]
        assert scope == {"limitations_are_authorization": False}
    assert legacy["unfinished_workflows"][0]["requested_scope"] == ""
    assert "CONFLICTING_MODEL_PROSE" not in json.dumps(legacy)
    with manager.store.transaction() as con:
        current = con.execute("SELECT * FROM agent_commands WHERE command_id='coverage-followup'").fetchone()
        public = task_command_projection(con, work, current)
        assert public["command"]["delivery"]["requested_scope"] == legacy_scope
        assert public["delivery_history"][0]["delivery"]["requested_scope"] == legacy_scope

    path.write_text(json.dumps({**metrics, "coverage_ok": True}))
    changed = json.loads(execution_context(config.paths, sid, "coverage-followup"))
    assert "observations" not in changed["producer_receipts"][0]


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

