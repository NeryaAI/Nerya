from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from nerya.agent.command_runtime import CommandRuntime
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.task_delivery import DeliveryError, finalize_delivery, start_delivery, update_delivery, record_delivery_verification
from nerya.tools.verification import verification_receipt
from nerya.tools.native import delivery as native_delivery
from nerya.tools.result_contracts import tool_json_data
from nerya.tools.types import ToolCall


TRACE = json.loads((Path(__file__).parent / "fixtures/delivery_acmd_253977467c6324cc69afd5f4.json").read_text())
pytestmark = pytest.mark.smoke


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("NETWORK_OR_REAL_MODEL_FORBIDDEN")
    monkeypatch.setattr("socket.socket.connect", denied)
    monkeypatch.setattr("socket.create_connection", denied)
    monkeypatch.setattr("nerya.llm.gateway.LLMGateway.call_messages", denied)


def _receipt(check_id="coverage", status="passed", revision="a", artifact_id="report:one", **extra):
    return verification_receipt(artifact_id=artifact_id, revision=revision, scope="Recorded producer checks",
                                checks=[{"check_id": check_id, "status": status, **extra}])


def _persist_replay_outputs(config, proposal, strategy_id, replay):
    """Use the real producer's persisted run/report shape in the fake replay."""
    from nerya.skills.builtin.backtest.scripts.run_state import RunTracker, atomic_json
    from nerya.skills.builtin.backtest.scripts.report import render_report
    root = config.paths.root / replay["run_receipt"]
    root.parent.mkdir(parents=True, exist_ok=True)
    tracker = RunTracker()
    tracker.bind(root.parent, strategy_id=strategy_id, proposal_id=proposal.id)
    tracker.complete(verdict="FAIL", coverage_complete=True)
    metrics = {**replay, "initial_capital_usd": 1000, "backtest_days": 30}
    atomic_json(root.with_name("metrics.json"), metrics)
    result = SimpleNamespace(strategy_id=strategy_id, trades=[])
    root.with_name("report.md").write_text(render_report(metrics, result, {}))
    replay["report_path"] = root.with_name("report.md").relative_to(config.paths.root).as_posix()


def _deps(config, command):
    from nerya.skills.registry import SkillRegistry
    from nerya.tools.native.skill import SkillIndex
    registry = SkillRegistry.load_builtin()
    config.data.setdefault("runtime", {})["command_id"] = command["command_id"]
    return SimpleNamespace(config=config, paths=config.paths, active_session_id=command["session_id"],
                           skill_index=SkillIndex([], registry_provider=lambda: registry))


@pytest.mark.parametrize("index", [0, 1, 2])
def test_real_acceptance_trace_rejects_ignored_fields_atomically(tmp_path, index):
    config, runtime = _runtime(tmp_path)
    command = _command(runtime)
    if index:
        start_delivery(config.paths, command_id=command["command_id"], session_id=command["session_id"],
                       skill_id="strategy_author", workflow_id="strategy_authoring",
                       requested_scope={"summary": "BTC EMA, 1h, 30 days"}, stages=_stages())
    args = deepcopy(TRACE[index]["arguments"])
    if index:
        args["expected_revision"] = 1
    handler = getattr(native_delivery, TRACE[index]["tool"] + "_handler")
    before = runtime.store.snapshot(command["session_id"])["commands"][0].get("delivery")
    result = handler(ToolCall(name=TRACE[index]["tool"], arguments=args), deps=_deps(config, command))
    assert result.is_error, "Malformed trace input must not be silently accepted"
    assert result.error.kind.value == "schema_validation"
    assert runtime.store.snapshot(command["session_id"])["commands"][0].get("delivery") == before


def test_finalize_incomplete_is_semantic_failure_with_recoverable_delivery(tmp_path):
    config, runtime = _runtime(tmp_path)
    command = _command(runtime)
    start_delivery(config.paths, command_id=command["command_id"], session_id=command["session_id"],
                   skill_id="strategy_author", workflow_id="strategy_authoring",
                   requested_scope={"summary": "replay"}, stages=_stages())
    result = native_delivery.delivery_finalize_handler(
        ToolCall(name="delivery_finalize", arguments={"expected_revision": 1}), deps=_deps(config, command))
    assert result.semantic_success is False
    data = tool_json_data(result)
    assert data["ok"] is False and data["status"] == "incomplete"
    assert data["completion"]["allowed"] is False
    assert data["delivery_revision"] == 2


def test_model_cannot_supply_passed_check(tmp_path):
    config, runtime = _runtime(tmp_path)
    command = _command(runtime)
    start_delivery(config.paths, command_id=command["command_id"], session_id=command["session_id"],
                   skill_id="strategy_author", workflow_id="strategy_authoring",
                   requested_scope={"summary": "replay"}, stages=_stages())
    result = native_delivery.delivery_update_handler(ToolCall(name="delivery_update", arguments={
        "expected_revision": 1, "checks": [{"check_id": "coverage", "status": "passed", "revision": "a"}],
    }), deps=_deps(config, command))
    assert result.is_error


def _runtime(tmp_path):
    config = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    return config, CommandRuntime(config, lambda _config, _request: {"stopped_reason": "end_turn", "final_text": "ok"}, epoch="delivery-test")


def _command(runtime, *, text="Research evidence", scopes=()):
    return runtime.submit({
        "command_id": "delivery-command", "session_id": "delivery-session",
        "_auth_actor_id": "operator", "_auth_scopes": list(scopes), "request": {"payload": {"text": text}},
    }, start=False)["command"]


def _strategy_command(config, runtime, *, base):
    """Fake only model extraction; admission, span checks and scope persistence are real."""
    from nerya.strategies.request_scope import ensure_contract
    from test_request_scope import FakeGateway, output

    text = f"Binance {base}/USDT EMA strategy, 1h candles, last 30 days; no trading."
    command = _command(runtime, text=text, scopes=["execute:strategy"])
    claimed = runtime.store.claim(command["session_id"], "delivery-fixture-worker")
    assert claimed and claimed["command_id"] == command["command_id"]
    _deps(config, command)
    config.data["llm"]["tiers"] = {"medium": {"provider": "openai", "model": "fixture-model",
                                             "allowed_tasks": ["agent.loop"]}}
    config.data["agent"]["native"]["tier"] = "medium"
    gateway = FakeGateway(lambda docs: output(docs[0], base=base))
    contract = ensure_contract(config, gateway=gateway)
    assert len(gateway.calls) == 1
    return command, contract


def _stages():
    return [
        {"id": "scope", "label": "Scope", "state": "completed"},
        {"id": "collect", "label": "Collect", "state": "in_progress", "required_artifacts": ["evidence-set"], "required_checks": ["coverage"]},
        {"id": "deliver", "label": "Deliver", "state": "pending"},
    ]


def test_start_is_idempotent_and_old_commands_have_no_delivery(tmp_path):
    config, runtime = _runtime(tmp_path)
    command = _command(runtime)
    first = start_delivery(config.paths, command_id=command["command_id"], session_id=command["session_id"], skill_id="research", workflow_id="research_brief", requested_scope={"summary": "current evidence"}, stages=_stages())
    second = start_delivery(config.paths, command_id=command["command_id"], session_id=command["session_id"], skill_id="research", workflow_id="research_brief", requested_scope={"summary": "current evidence"}, stages=_stages())
    assert second == first
    assert runtime.store.snapshot(command["session_id"])["commands"][0]["delivery"]["delivery_id"] == first["delivery_id"]


def test_completion_requires_artifact_and_check_and_supports_partial(tmp_path):
    config, runtime = _runtime(tmp_path)
    command = _command(runtime)
    delivery = start_delivery(config.paths, command_id=command["command_id"], session_id=command["session_id"], skill_id="backtest", workflow_id="strategy_replay", requested_scope={"summary": "replay"}, stages=_stages())
    assert delivery["completion"]["allowed"] is False
    delivery = update_delivery(config.paths, command_id=command["command_id"], session_id=command["session_id"], expected_revision=delivery["delivery_revision"], stages=[{"id": "collect", "state": "completed"}, {"id": "deliver", "state": "completed"}], artifacts=["evidence-set"], producer_receipt=_receipt(artifact_id="evidence-set"), limitations=["no_out_of_sample_verification"])
    assert delivery["completion"]["allowed"] is False
    assert "trusted_delivery_contract_missing" in delivery["completion"]["reasons"]
    assert delivery["completion"]["status"] == "partial"


def test_revision_conflict_and_source_revision_stale(tmp_path):
    config, runtime = _runtime(tmp_path)
    command = _command(runtime)
    delivery = start_delivery(config.paths, command_id=command["command_id"], session_id=command["session_id"], skill_id="strategy_author", workflow_id="strategy_authoring", requested_scope={"summary": "candidate"}, stages=[{"id": "validate", "state": "completed", "required_checks": ["source"]}])
    with pytest.raises(DeliveryError, match="delivery_revision_conflict"):
        update_delivery(config.paths, command_id=command["command_id"], session_id=command["session_id"], expected_revision=delivery["delivery_revision"] + 1, checks=[])
    delivery = update_delivery(config.paths, command_id=command["command_id"], session_id=command["session_id"], expected_revision=delivery["delivery_revision"], producer_receipt=_receipt("source"))
    delivery = update_delivery(config.paths, command_id=command["command_id"], session_id=command["session_id"], expected_revision=delivery["delivery_revision"], producer_receipt=_receipt("source", "pending", "b"))
    assert delivery["checks"][0]["status"] == "pending"


def test_delivery_events_are_durable(tmp_path):
    config, runtime = _runtime(tmp_path)
    command = _command(runtime)
    delivery = start_delivery(config.paths, command_id=command["command_id"], session_id=command["session_id"], skill_id="research", workflow_id="research_brief", requested_scope={"summary": "research"}, stages=[{"id": "scope"}])
    finalize_delivery(config.paths, command_id=command["command_id"], session_id=command["session_id"], expected_revision=delivery["delivery_revision"])
    events = runtime.store.events(command["session_id"], command["command_id"])["events"]
    assert any(event["kind"] == "delivery.stage" for event in events)
    assert any(event["kind"] == "delivery.check" for event in events) is False


def test_stage_patch_retains_requirements_and_required_checks_are_global(tmp_path):
    config, runtime = _runtime(tmp_path)
    command = _command(runtime)
    first = start_delivery(config.paths, command_id=command["command_id"], session_id=command["session_id"],
                           skill_id="backtest", workflow_id="strategy_replay", requested_scope={"summary": "replay"}, stages=_stages())
    updated = update_delivery(config.paths, command_id=command["command_id"], session_id=command["session_id"],
                              expected_revision=first["delivery_revision"], stages=[{"id": "collect", "state": "completed"}, {"id": "deliver", "state": "completed"}],
                              producer_receipt=_receipt("behavioral_tests", "failed"))
    assert updated["stages"][1]["required_checks"] == ["coverage"]
    assert updated["stages"][1]["required_artifacts"] == ["evidence-set"]
    assert set(updated["completion"]["reasons"]) == {"trusted_delivery_contract_missing", "producer_evidence_missing", "verified_artifact_missing", "artifact_missing:evidence-set", "check_missing:coverage", "check_failed:behavioral_tests"}


def test_approval_then_question_inherits_delivery_with_current_command_cas(tmp_path, monkeypatch):
    from nerya.agent.interactions import create_interaction, respond
    from nerya.core import jsonl
    from nerya.db.repositories import AgentSessionRepository
    from nerya.strategies.tool_verification import current_command_delivery_receipts
    from nerya.task_delivery import read_delivery

    config, runtime = _runtime(tmp_path)
    monkeypatch.setattr(runtime, "kick", lambda _: None)
    command = _command(runtime)
    cid, sid = command["command_id"], command["session_id"]
    original = runtime.store.claim(sid, "owner")
    row = start_delivery(config.paths, command_id=cid, session_id=sid, skill_id="backtest",
        workflow_id="strategy_replay", requested_scope={"summary": "replay"}, stages=_stages())
    receipt = _receipt("behavioral_tests", artifact_id="strategy:candidate:proposal:installed")
    row = record_delivery_verification(config.paths, command_id=cid, receipt=receipt)
    runtime.store.record_event(cid, {"event_id": "original-validation", "kind": "tool.complete",
        "session_id": sid, "turn_id": original["turn_id"], "action": "strategy_validate",
        "verification": receipt})
    runtime.store.finish(cid, sid, "owner", "awaiting_approval")
    approval = {"approval_id": "delivery-approval", "state": "approved",
        "kind": "tool_permission_batch", "requester_actor_id": "operator",
        "requester_session_id": sid, "turn_id": original["turn_id"]}
    jsonl.append(config.paths.approvals_approved, approval)
    approved = runtime.resolve_approval(approval, start=False)["command"]
    inherited = read_delivery(config.paths, command_id=approved["command_id"], session_id=sid)
    assert inherited == {**row, "command_id": approved["command_id"]}
    resumed = runtime.store.claim(sid, "owner")
    assert resumed["command_id"] == approved["command_id"]
    assert runtime.store.snapshot(sid, resumed["command_id"])["command"]["delivery"] == inherited
    updated = update_delivery(config.paths, command_id=resumed["command_id"], session_id=sid,
        expected_revision=inherited["delivery_revision"], limitations=["No live trading"])
    assert updated["delivery_revision"] == inherited["delivery_revision"] + 1
    with pytest.raises(DeliveryError, match="delivery_revision_conflict"):
        update_delivery(config.paths, command_id=resumed["command_id"], session_id=sid,
            expected_revision=inherited["delivery_revision"], limitations=[])
    question = create_interaction(config, sid=sid, tid=resumed["turn_id"], call_id="delivery-question",
        kind="question", payload={"title": "Continue?"})
    with runtime.store.transaction() as con:
        AgentSessionRepository(con).save_turn_checkpoint(session_id=sid,
            turn_id=resumed["turn_id"], checkpoint={"resumable": True})
    runtime.store.finish(resumed["command_id"], sid, "owner", "awaiting_input")
    answer = respond(runtime, {"session_id": sid, "interaction_id": question["interaction_id"],
        "response_id": "delivery-answer", "expected_revision": question["revision"],
        "_auth_actor_id": "operator", "action": "answer", "text": "Continue"})["command"]
    claimed_answer = runtime.store.claim(sid, "owner")
    assert claimed_answer["command_id"] == answer["command_id"]
    assert runtime.store.snapshot(sid, answer["command_id"])["command"]["delivery"] == {**updated, "command_id": answer["command_id"]}
    inherited = read_delivery(config.paths, command_id=answer["command_id"], session_id=sid)
    assert inherited == {**updated, "command_id": answer["command_id"]}
    receipts, _ = current_command_delivery_receipts(config, answer["command_id"], sid)
    assert receipts == [receipt]
    finalized = finalize_delivery(config.paths, command_id=answer["command_id"], session_id=sid,
        expected_revision=inherited["delivery_revision"], producer_receipts=receipts)
    assert finalized["delivery_id"] == row["delivery_id"]
    assert finalized["command_id"] == answer["command_id"]
    assert finalized["checks"] == row["checks"]
    assert read_delivery(config.paths, command_id=cid, session_id=sid) == row
    unrelated = runtime.submit({"command_id": "unrelated-delivery", "session_id": sid,
        "_auth_actor_id": "operator", "request": {"payload": {"text": "New task"}}}, start=False)["command"]
    with pytest.raises(DeliveryError, match="delivery_not_started"):
        read_delivery(config.paths, command_id=unrelated["command_id"], session_id=sid)


def test_finalize_scopes_invalidation_without_staling_unchanged_source(tmp_path):
    from nerya.strategies.tool_verification import delivery_source_versions, source_binding

    config, runtime = _runtime(tmp_path)
    command = _command(runtime)
    root = config.paths.strategy("candidate")
    root.mkdir(parents=True)
    source = root / "main.py"
    source.write_text("def run(ctx): return None\n")
    call = ToolCall(name="strategy_validate", arguments={"strategy_id": root.name})
    binding = source_binding(config, call)
    receipt = verification_receipt(**binding, scope="Producer checks", checks=[
        {"check_id": name, "status": status, "evidence_ref": ref}
        for name, status, ref in [
            ("source_binding", "passed", "strategies/candidate"),
            ("behavioral_tests", "passed", ""),
            ("candidate_scope_match", "passed", "request_scope:obsolete"),
            ("request_scope_match", "missing", ""),
            ("visualization_output", "passed", "missing-output"),
        ]
    ])
    row = start_delivery(config.paths, command_id=command["command_id"], session_id=command["session_id"],
        skill_id="backtest", workflow_id="strategy_replay", requested_scope={"summary": "replay"},
        stages=[{"id": "deliver", "state": "completed"}])
    row = record_delivery_verification(config.paths, command_id=command["command_id"], receipt=receipt)
    output = root / "backtests/saved"
    output.mkdir(parents=True)
    (output / "metrics.json").write_text('{}\n')
    (output / "report.md").write_text('Generated replay report\n')
    assert source_binding(config, call) == binding
    versions = delivery_source_versions(config, row)
    assert versions[binding["artifact_id"]] == binding["revision"]
    row = finalize_delivery(config.paths, command_id=command["command_id"], session_id=command["session_id"],
        expected_revision=row["delivery_revision"], source_versions=versions, producer_receipts=[receipt])
    statuses = {c["check_id"]: c["status"] for c in row["checks"]}
    assert statuses == {"source_binding": "passed", "behavioral_tests": "passed",
        "candidate_scope_match": "stale", "request_scope_match": "missing", "visualization_output": "stale"}
    assert not row["completion"]["allowed"]
    assert "check_stale:visualization_output" in row["completion"]["reasons"]
    assert "check_missing:request_scope_match" in row["completion"]["reasons"]
    source.write_text("def run(ctx): return ctx.result.hold()\n")
    row = finalize_delivery(config.paths, command_id=command["command_id"], session_id=command["session_id"],
        expected_revision=row["delivery_revision"], source_versions=delivery_source_versions(config, row),
        producer_receipts=[receipt])
    assert all(c["status"] == "stale" for c in row["checks"]
               if c["check_id"] in {"source_binding", "behavioral_tests"})
    assert not row["completion"]["allowed"]


def test_receipts_keep_artifacts_separate_and_invalidate_prior_versions(tmp_path):
    config, runtime = _runtime(tmp_path)
    command = _command(runtime)
    start_delivery(config.paths, command_id=command["command_id"], session_id=command["session_id"],
                   skill_id="backtest", workflow_id="strategy_replay", requested_scope={"summary": "replay"}, stages=[{"id": "deliver", "state": "completed"}])
    for receipt in (_receipt("behavioral_tests", "failed"), _receipt("behavioral_tests", artifact_id="report:two"),
                    _receipt("coverage"), _receipt("behavioral_tests", revision="b")):
        delivery = record_delivery_verification(config.paths, command_id=command["command_id"], receipt=receipt)
    checks = {(row["artifact_id"], row["check_id"]): row["status"] for row in delivery["checks"]}
    assert checks == {("report:one", "behavioral_tests"): "passed", ("report:two", "behavioral_tests"): "passed", ("report:one", "coverage"): "stale"}
    assert delivery["completion"]["allowed"] is False


def test_producer_receipt_replaces_obsolete_report_reference(tmp_path):
    config, runtime = _runtime(tmp_path)
    command = _command(runtime)
    delivery = start_delivery(config.paths, command_id=command["command_id"],
        session_id=command["session_id"], skill_id="backtest", workflow_id="strategy_replay",
        requested_scope={"summary": "replay"}, stages=[{"id": "readback",
            "required_checks": ["report_output"], "required_artifacts": ["report"]}])
    update_delivery(config.paths, command_id=command["command_id"],
        session_id=command["session_id"], expected_revision=delivery["delivery_revision"],
        artifacts=[{"artifact_id": "report", "ref": "backtests/old/diagnostic.md"}])
    receipt = _receipt("report_output", revision="b", artifact_id="strategy:candidate",
                       evidence_ref="backtests/current/report.md")
    delivery = record_delivery_verification(config.paths, command_id=command["command_id"], receipt=receipt)
    report = next(row for row in delivery["artifacts"] if row["artifact_id"] == "report")
    assert (report["ref"], report["revision"]) == ("backtests/current/report.md", "b")
    assert "artifact_unverified:report" not in delivery["completion"]["reasons"]
    missing = _receipt("report_output", "missing", revision="b",
                       artifact_id="strategy:candidate", evidence_ref="backtests/failed/report.md")
    delivery = record_delivery_verification(config.paths, command_id=command["command_id"], receipt=missing)
    assert delivery["artifacts"][0]["ref"] == "backtests/current/report.md"
    assert delivery["completion"]["allowed"] is False


@pytest.mark.parametrize("field,value", [("stages", [True]), ("checks", ["passed"]), ("artifacts", [{"type": "report", "ref": "x"}]),
                                         ("recovery", {"safe_to_retry": "true"}), ("expected_revision", True),
                                         ("producer_receipt", {"checks": [{"status": "passed"}]}),
                                         ("source_versions", {"strategy:x": "forged"})])
def test_nested_schema_and_host_only_fields_are_rejected(tmp_path, field, value):
    config, runtime = _runtime(tmp_path)
    command = _command(runtime)
    result = native_delivery.delivery_update_handler(ToolCall(name="delivery_update", arguments={"expected_revision": 1, field: value}), deps=_deps(config, command))
    assert result.is_error and result.error.kind.value == "schema_validation"


@pytest.mark.parametrize("behavioral,expected", [({"status": "passed", "exit_code": 0}, True),
    ({"status": "failed", "exit_code": 1, "summary": {"failed": 2, "passed": 3}}, False),
    ({"status": "not_run", "exit_code": None}, False)])
def test_strategy_producers_deliver_loss_without_certifying_missing_tests(tmp_path, monkeypatch, behavioral, expected):
    from nerya.core import yaml_io
    from nerya.evolution.patch_proposal import create_proposal
    from nerya.strategies.request_scope import compare
    from nerya.strategies.tool_verification import source_binding
    from nerya.tools.native import strategy_runtime
    from nerya.tools.types import ToolResult

    config, runtime = _runtime(tmp_path)
    command, contract = _strategy_command(config, runtime, base="BTC")
    deps = _deps(config, command)
    stages = [{"id": name, "state": "pending"} for name in ("scope", "author", "validate", "replay", "receipt", "deliver")]
    stages[2]["required_checks"] = ["strategy_contract", "behavioral_tests"]
    stages[3]["required_checks"] = ["historical_run", "data_coverage", "historical_data", "replay_source", "execution_integrity"]
    stages[4]["required_artifacts"] = ["candidate", "report", "run_receipt"]
    start = native_delivery.delivery_start_handler(ToolCall(name="delivery_start", arguments={
        "skill_id": "strategy_author", "workflow_id": "strategy_authoring",
        "requested_scope": {"summary": "BTC EMA, 1h, 30 days; no live trading"}, "stages": stages,
    }), deps=deps)
    assert not start.is_error
    manifest = {"id": "btc_ema_cross_1h", "mode": "paper", "markets": ["BINANCE:BTC/USDT"],
                "timeframe": "1h", "backtest": {"window_days": 30}}
    files = {"main.py": "def run(ctx): return ctx.result.hold(reason='no_cross')\n",
             "strategy.yml": yaml_io.dumps(manifest)}
    proposal = create_proposal(config.paths, kind="strategy_package_proposal", summary="candidate", rationale="test", test_plan="test", rollback="test",
                               target="strategies/btc_ema_cross_1h", extra_files={f"after/strategies/btc_ema_cross_1h/{name}": content for name, content in files.items()})
    args = {"proposal_id": proposal.id, "strategy_id": "btc_ema_cross_1h"}
    binding = source_binding(config, ToolCall(name="strategy_validate", arguments=args))
    assert binding["revision"] != "unbound"
    validation = {"ok": expected, "structural": {"status": "passed"}, "behavioral": behavioral, "blockers": []}
    def inline(call, **_):
        return ToolResult.from_json(tool_use_id=call.id, name=call.name, data={**args, "validation": validation, "state": "pending_review"})
    monkeypatch.setattr(strategy_runtime, "_strategy_draft_proposal_handler", inline)
    monkeypatch.setattr(strategy_runtime, "_strategy_submit_proposal_handler", inline)
    for name in ("strategy_draft_proposal", "strategy_submit_proposal"):
        result = getattr(strategy_runtime, name + "_handler")(ToolCall(name=name, arguments=args), config=config)
        assert result.metadata["verification"]["revision"] == binding["revision"]
        assert tool_json_data(result)["delivery_revision"] >= 2
    start, end = contract["constraints"]["start_utc"], contract["constraints"]["end_utc"]
    replay = {"ok": True, "backtest_status": "completed", "verdict": "FAIL", "total_return_pct": -6.704160826018433,
              **compare(contract, manifest),
              "requested_window_complete": True, "performance_evidence": True, "run_receipt": "backtests/20261021_172222/run.json",
              "replay": {"errors": 0, "sdk_errors": 0, "order_accounting_ok": True, "protective_closes": 0, "forced_closes": 1},
              "provenance": {"data_kind": "historical", "source_revision": binding["revision"], "source_changed_during_run": False,
                  "datasets": [{"market": "BINANCE:BTC/USDT", "timeframe": "1h", "rows": 720, "first_ts": start,
                                "last_ts": end - 3600, "duplicate_timestamps": 0, "out_of_order": False, "sha256": "d" * 64}]}}
    _persist_replay_outputs(config, proposal, "btc_ema_cross_1h", replay)
    # The actual acceptance result has 50 scalar fields before producer metadata.
    replay.update({f"report_field_{index}": index for index in range(60)})
    monkeypatch.setattr(strategy_runtime, "_strategy_backtest_handler", lambda call, **_: ToolResult.from_json(tool_use_id=call.id, name=call.name, data=replay))
    result = strategy_runtime.strategy_backtest_handler(ToolCall(name="strategy_backtest", arguments=args), config=config)
    assert result.semantic_success is True  # Economic FAIL is independent.
    from nerya.llm.tool_compaction import compact_tool_result
    payload = tool_json_data(result)
    compacted = compact_tool_result("strategy_backtest", payload, size_threshold=1)
    assert compacted.kept["delivery_revision"] == payload["delivery_revision"]
    updated = native_delivery.delivery_update_handler(ToolCall(name="delivery_update", arguments={
        "expected_revision": tool_json_data(result)["delivery_revision"],
        "stages": [{"id": row["id"], "state": "completed"} for row in stages],
        "artifacts": [{"artifact_id": "report", "kind": "backtest_report", "ref": "backtests/20261021_172222/report.md"},
                      {"artifact_id": "candidate", "ref": (proposal.path / "after/strategies/btc_ema_cross_1h").relative_to(config.paths.root).as_posix()},
                      {"artifact_id": "run_receipt", "ref": replay["run_receipt"]}],
        "limitations": ["Historical return -6.7042%; no live execution"],
    }), deps=deps)
    assert not updated.is_error
    finalized = native_delivery.delivery_finalize_handler(ToolCall(name="delivery_finalize", arguments={"expected_revision": tool_json_data(updated)["delivery_revision"]}), deps=deps)
    final = tool_json_data(finalized)
    assert finalized.semantic_success is expected
    assert final["completion"]["allowed"] is expected
    assert all(stage["state"] == "completed" for stage in final["stages"])
    checks = {row["check_id"]: row for row in final["checks"]}
    assert checks["strategy_contract"]["status"] == "passed"
    assert checks["historical_run"]["status"] == "passed"
    assert checks["report_output"]["status"] == "passed"
    assert checks["request_scope_match"]["status"] == "passed"
    assert checks["request_scope_match"]["required"] and checks["request_scope_match"]["producer_owned"]
    behavioral_status = "passed" if expected else "missing" if behavioral["status"] == "not_run" else "failed"
    assert checks["behavioral_tests"]["status"] == behavioral_status
    if not expected:
        assert final["completion"]["reasons"] == [f"check_{behavioral_status}:behavioral_tests"]
    # Finalization re-reads source identities even without another producer call.
    (proposal.path / "after/strategies/btc_ema_cross_1h/main.py").write_text("# source edited after verification\n")
    stale = native_delivery.delivery_finalize_handler(ToolCall(name="delivery_finalize", arguments={"expected_revision": final["delivery_revision"]}), deps=deps)
    assert stale.semantic_success is False
    stale_delivery = tool_json_data(stale)
    assert "check_stale:historical_run" in stale_delivery["completion"]["reasons"]
    stale_checks = {row["check_id"]: row["status"] for row in stale_delivery["checks"]}
    assert stale_checks["request_scope_match"] == "stale"
    assert stale_checks["behavioral_tests"] == ("stale" if expected else behavioral_status)


def test_failed_submit_keeps_producer_failure_in_delivery(tmp_path, monkeypatch):
    from nerya.tools.native import strategy_runtime
    from nerya.strategies import tool_verification
    from nerya.tools.types import ToolError, ToolErrorKind, ToolResult
    config, runtime = _runtime(tmp_path)
    command = _command(runtime)
    _deps(config, command)
    start_delivery(config.paths, command_id=command["command_id"], session_id=command["session_id"],
                   skill_id="strategy_author", workflow_id="strategy_authoring", requested_scope={"summary": "candidate"},
                   stages=[{"id": "validate", "state": "completed", "required_checks": ["behavioral_tests"]}])
    binding = {"artifact_id": "strategy:btc:proposal:prp_test", "revision": "a" * 64}
    monkeypatch.setattr(tool_verification, "source_binding", lambda *_: binding)
    def failed(call, **_):
        return ToolResult.from_error(tool_use_id=call.id, name=call.name, error=ToolError(kind=ToolErrorKind.SCHEMA_VALIDATION,
            message="candidate tests failed", recovery_hint={"proposal_id": "prp_test", "strategy_id": "btc", "validation": {
                "ok": False, "structural": {"status": "passed"}, "behavioral": {"status": "failed", "exit_code": 1, "summary": {"passed": 3, "failed": 2}}}}))
    monkeypatch.setattr(strategy_runtime, "_strategy_submit_proposal_handler", failed)
    result = strategy_runtime.strategy_submit_proposal_handler(ToolCall(name="strategy_submit_proposal", arguments={"proposal_id": "prp_test"}), config=config)
    assert result.is_error and result.semantic_success is False
    assert tool_json_data(result)["delivery_revision"] == 2
    delivery = runtime.store.snapshot(command["session_id"])["commands"][0]["delivery"]
    assert delivery["completion"]["allowed"] is False
    assert "check_failed:behavioral_tests" in delivery["completion"]["reasons"]


def test_model_cannot_clear_failed_producer_requirement(tmp_path):
    config, runtime = _runtime(tmp_path)
    command = _command(runtime)
    deps = _deps(config, command)
    start_delivery(config.paths, command_id=command["command_id"], session_id=command["session_id"],
                   skill_id="strategy_author", workflow_id="strategy_authoring", requested_scope={"summary": "candidate"},
                   stages=[{"id": "validate", "state": "completed"}])
    record_delivery_verification(config.paths, command_id=command["command_id"], receipt=_receipt("behavioral_tests", "failed"))
    result = native_delivery.delivery_update_handler(ToolCall(name="delivery_update", arguments={"expected_revision": 2,
        "checks": [{"check_id": "behavioral_tests", "status": "missing", "required": False}]}), deps=deps)
    assert result.is_error and "owned_by_producer" in result.error.message
    assert "read-only" in result.error.message and "omit checks" in result.error.message
    assert runtime.store.snapshot(command["session_id"])["commands"][0]["delivery"]["delivery_revision"] == 2


def test_compacted_finalize_retains_semantic_failure(tmp_path):
    from nerya.agent.tool_projection import project_tool_results
    config, runtime = _runtime(tmp_path)
    command = _command(runtime)
    start_delivery(config.paths, command_id=command["command_id"], session_id=command["session_id"],
                   skill_id="research", workflow_id="research_brief", requested_scope={"summary": "evidence"}, stages=[{"id": "collect"}])
    result = native_delivery.delivery_finalize_handler(ToolCall(name="delivery_finalize", arguments={"expected_revision": 1}), deps=_deps(config, command))
    projection = project_tool_results([result], render_tool_result=lambda _: {"content": [], "compaction": {"rule_id": "test"}},
                                      rendered_tool_result_text=lambda _: "[compacted]")
    assert projection.event_blocks[0]["semantic_success"] is False


def _completed_replay_before_delivery(config, runtime, command, *, contract=None):
    from nerya.agent.tool_projection import project_tool_results
    from nerya.core import yaml_io
    from nerya.evolution.patch_proposal import create_proposal
    from nerya.strategies.request_scope import compare
    from nerya.strategies.tool_verification import attach_strategy_checks, source_binding
    from nerya.tools.types import ToolResult

    deps = _deps(config, command)
    sid = "eth_ema_trend_1h"
    manifest = {"id": sid, "mode": "paper", "markets": ["BINANCE:ETH/USDT"],
                "timeframe": "1h", "backtest": {"window_days": 30}}
    proposal = create_proposal(config.paths, kind="strategy_package_proposal", summary="candidate",
        rationale="test", test_plan="test", rollback="test", target=f"strategies/{sid}",
        extra_files={f"after/strategies/{sid}/main.py": "def run(ctx): return ctx.result.hold()\n",
                     f"after/strategies/{sid}/strategy.yml": yaml_io.dumps(manifest)})
    args = {"strategy_id": sid, "proposal_id": proposal.id}
    call = ToolCall(id="completed-replay", name="strategy_backtest", arguments=args)
    binding = source_binding(config, call)
    start, end = ((contract["constraints"]["start_utc"], contract["constraints"]["end_utc"])
                  if contract else (1, 2768401))
    report = {"ok": True, "backtest_status": "completed", "verdict": "FAIL", "total_return_pct": -6.7,
        **compare(contract, manifest),
        "requested_window_complete": True, "performance_evidence": True,
        "run_receipt": "backtests/20261022_040449/run.json",
        "replay": {"errors": 0, "sdk_errors": 0, "order_accounting_ok": True},
        "provenance": {"data_kind": "historical", "source_revision": binding["revision"],
            "source_changed_during_run": False, "datasets": [{"market": "BINANCE:ETH/USDT",
                "timeframe": "1h", "rows": 720, "first_ts": start, "last_ts": end - 3600,
                "duplicate_timestamps": 0, "out_of_order": False, "sha256": "d" * 64}]}}
    _persist_replay_outputs(config, proposal, sid, report)
    result = attach_strategy_checks(ToolResult.from_json(tool_use_id=call.id, name=call.name, data=report),
        before=binding, after=binding, kind="backtest", config=config)
    projection = project_tool_results([result], render_tool_result=lambda _: {"content": [],
        "compaction": {"rule_id": "test"}}, rendered_tool_result_text=lambda _: "[compacted]")
    block = projection.event_blocks[0]
    event = {**block, "event_id": "completed-replay-event", "kind": "tool.complete",
        "session_id": command["session_id"], "turn_id": command["turn_id"], "payload": args}
    runtime.store.record_event(command["command_id"], event)
    stages = [{"id": "replay", "state": "completed",
        "required_checks": [row["check_id"] for row in block["verification"]["checks"] if row["required"]],
        "required_artifacts": ["report"]}, {"id": "deliver", "state": "pending"}]
    start = {"skill_id": "backtest", "workflow_id": "strategy_replay",
             "requested_scope": {"summary": "ETH EMA 1h, historical 30 day report"}, "stages": stages}
    return deps, proposal, event, start


@pytest.mark.parametrize("late_start", [True, False])
def test_reconcile_current_completed_replay_without_rerun_or_auto_stages(tmp_path, monkeypatch, late_start):
    from nerya.evolution.patch_proposal import list_proposals
    from nerya.tools.native import strategy_runtime

    config, runtime = _runtime(tmp_path)
    command, contract = _strategy_command(config, runtime, base="ETH")
    deps, proposal, event, args = _completed_replay_before_delivery(config, runtime, command, contract=contract)
    scope_check = next(row for row in event["verification"]["checks"] if row["check_id"] == "request_scope_match")
    assert scope_check["status"] == "passed" and scope_check["required"]
    def no_replay(*_, **__):
        pytest.fail("Reconciliation must not execute a strategy")
    monkeypatch.setattr(strategy_runtime, "strategy_backtest_handler", no_replay)
    if late_start:
        delivery = tool_json_data(native_delivery.delivery_start_handler(
            ToolCall(name="delivery_start", arguments=args), deps=deps))
        assert delivery["checks"], "Late start lost durable producer checks"
        repeated = tool_json_data(native_delivery.delivery_start_handler(
            ToolCall(name="delivery_start", arguments=args), deps=deps))
        assert repeated == delivery
    else:
        # Existing late-start delivery on a runtime predating reconciliation.
        delivery = start_delivery(config.paths, command_id=command["command_id"],
                                  session_id=command["session_id"], **args)
        delivery = update_delivery(config.paths, command_id=command["command_id"],
            session_id=command["session_id"], expected_revision=delivery["delivery_revision"],
            checks=[{"check_id": row["check_id"], "status": "pending"} for row in event["verification"]["checks"]])
    delivery = update_delivery(config.paths, command_id=command["command_id"], session_id=command["session_id"],
        expected_revision=delivery["delivery_revision"], artifacts=[{"artifact_id": "report",
            "ref": "backtests/20261022_040449/report.md"},
            {"artifact_id": "run_receipt", "ref": "backtests/20261022_040449/run.json"}],
        limitations=["Historical economic FAIL; no live trading"])
    final = tool_json_data(native_delivery.delivery_finalize_handler(ToolCall(name="delivery_finalize",
        arguments={"expected_revision": delivery["delivery_revision"]}), deps=deps))
    assert final["completion"]["reasons"] == ["required_stage_pending"]
    assert final["stages"][1]["state"] == "pending"
    assert len(final["checks"]) == len(event["verification"]["checks"])
    assert all(row["status"] == "passed" and row["producer_owned"] for row in final["checks"])
    updated = update_delivery(config.paths, command_id=command["command_id"], session_id=command["session_id"],
        expected_revision=final["delivery_revision"], stages=[{"id": "deliver", "state": "completed"}])
    final = tool_json_data(native_delivery.delivery_finalize_handler(ToolCall(name="delivery_finalize",
        arguments={"expected_revision": updated["delivery_revision"]}), deps=deps))
    assert final["completion"]["allowed"] is True
    assert final["completion"]["status"] == "partial"
    assert next(p for p in list_proposals(config.paths) if p.id == proposal.id).state == proposal.state
    assert not (config.paths.strategies / "eth_ema_trend_1h/main.py").exists()


@pytest.mark.parametrize("change", ["session", "turn", "action", "namespace", "content_only"])
def test_reconcile_never_promotes_foreign_or_content_receipts(tmp_path, change):
    config, runtime = _runtime(tmp_path)
    command = _command(runtime)
    deps, proposal, event, args = _completed_replay_before_delivery(config, runtime, command)
    if change == "session":
        event["session_id"] = "another-session"
    elif change == "turn":
        event["turn_id"] = "another-turn"
    elif change == "action":
        event["action"] = "read_file"
    elif change == "namespace":
        event["skill_id"] = "mcp"
    else:
        event["result"] = {"verification": event.pop("verification")}
    with runtime.store.transaction() as con:
        con.execute("UPDATE agent_command_events SET payload_json=? WHERE command_id=? AND event_id=?",
                    (json.dumps(event), command["command_id"], event["event_id"]))
    result = native_delivery.delivery_start_handler(ToolCall(name="delivery_start", arguments=args), deps=deps)
    assert not result.is_error
    assert tool_json_data(result)["checks"] == []


@pytest.mark.parametrize("verified_scope", [True, False])
def test_reconciled_receipts_are_stale_after_source_edit_and_cas_is_preserved(tmp_path, verified_scope):
    config, runtime = _runtime(tmp_path)
    command, contract = _strategy_command(config, runtime, base="ETH") if verified_scope else (_command(runtime), None)
    deps, proposal, event, args = _completed_replay_before_delivery(config, runtime, command, contract=contract)
    main = proposal.path / "after/strategies/eth_ema_trend_1h/main.py"
    main.write_text("def run(ctx): return ctx.result.hold(reason='edited')\n")
    delivery = tool_json_data(native_delivery.delivery_start_handler(ToolCall(name="delivery_start", arguments=args), deps=deps))
    original = {row["check_id"]: row["status"] for row in event["verification"]["checks"]}
    checks = {row["check_id"]: row for row in delivery["checks"]}
    assert checks.keys() == original.keys()
    assert original["request_scope_match"] == ("passed" if verified_scope else "missing")
    assert all(row["producer_owned"] and row["status"] == ("stale" if original[key] == "passed" else original[key])
               for key, row in checks.items())
    reasons = delivery["completion"]["reasons"]
    assert delivery["completion"]["allowed"] is False
    assert "check_stale:source_binding" in reasons
    assert f"check_{'stale' if verified_scope else 'missing'}:request_scope_match" in reasons
    revision = delivery["delivery_revision"]
    updated = update_delivery(config.paths, command_id=command["command_id"], session_id=command["session_id"],
                              expected_revision=revision, limitations=["current"])
    conflict = native_delivery.delivery_finalize_handler(ToolCall(name="delivery_finalize",
        arguments={"expected_revision": revision}), deps=deps)
    assert conflict.is_error and "revision_conflict" in conflict.text()
    from nerya.task_delivery import read_delivery
    assert read_delivery(config.paths, command_id=command["command_id"], session_id=command["session_id"]) == updated
