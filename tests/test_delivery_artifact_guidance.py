"""Model-visible delivery recovery through the real renderer; no live services."""
import json
from types import SimpleNamespace

import pytest

from nerya.agent.loop import WorkspaceNativeAgentLoop
from nerya.evolution.patch_proposal import create_proposal
from nerya.strategies.tool_verification import source_binding
from nerya.task_delivery import read_delivery, record_delivery_verification
from nerya.tools.native import delivery
from nerya.tools.result_contracts import tool_json_data
from nerya.tools.types import ToolCall
from test_task_delivery import _command, _deps, _receipt, _runtime

pytestmark = pytest.mark.smoke


def _call(deps, name, args):
    return getattr(delivery, name + "_handler")(ToolCall(name=name, arguments=args), deps=deps)


def _render(result, root=None):
    # No loop startup or fake renderer: exercise the actual model injection seam.
    loop = object.__new__(WorkspaceNativeAgentLoop)
    loop.config = SimpleNamespace(workspace_root=root)
    block = loop._render_tool_result(result)
    return block, "\n".join(part["text"] for part in block["content"])


@pytest.fixture
def env(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("NETWORK_OR_MODEL_FORBIDDEN")
    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    monkeypatch.setattr("nerya.llm.gateway.LLMGateway.call_messages", forbidden)
    config, runtime = _runtime(tmp_path)
    command = _command(runtime)
    deps = _deps(config, command)
    started = _call(deps, "delivery_start", {
        "skill_id": "strategy_author", "workflow_id": "strategy_authoring",
        "requested_scope": {"summary": "LP candidate; historical pool data missing"},
        "stages": [{"id": "validate", "state": "completed", "required_artifacts": ["candidate"]},
                   {"id": "readback", "state": "completed", "required_artifacts": ["report", "run_receipt"]}],
    })
    assert not started.is_error
    proposal = create_proposal(config.paths, kind="strategy_package_proposal", summary="candidate",
        rationale="test", test_plan="test", rollback="test", target="strategies/lp_candidate",
        extra_files={"after/strategies/lp_candidate/main.py": "def run(ctx): return ctx.result.hold()\n",
                     "after/strategies/lp_candidate/strategy.yml": "id: lp_candidate\nmode: paper\n"})
    binding = source_binding(config, ToolCall(name="strategy_validate", arguments={
        "strategy_id": "lp_candidate", "proposal_id": proposal.id}))
    row = record_delivery_verification(config.paths, command_id=command["command_id"],
        receipt=_receipt("source_binding", **binding))
    return config, command, deps, binding, row


def _read(env):
    config, command, *_ = env
    return read_delivery(config.paths, command_id=command["command_id"], session_id=command["session_id"])


def test_rejected_pass_is_recoverable_in_actual_model_render(env):
    _, _, deps, binding, row = env
    rejected = _call(deps, "delivery_update", {"expected_revision": row["delivery_revision"],
        "checks": [{"check_id": "source_binding", "status": "passed"}]})
    assert rejected.is_error
    block, text = _render(rejected)
    assert block["is_error"] is True
    # Recovery metadata alone is insufficient: the real renderer discards it.
    assert '"expected_revision":' in text and str(row["delivery_revision"]) in text
    assert '"required_artifacts":["candidate","report","run_receipt"]' in text
    assert "producer-owned" in text and "IDs, not paths" in text
    candidate = rejected.error.recovery_hint["artifact_examples"][0]
    assert candidate == {"artifact_id": "candidate", "ref": binding["artifact_id"], "revision": binding["revision"]}
    assert json.dumps(candidate, separators=(",", ":")) in text
    assert _read(env) == row


def test_candidate_alias_does_not_attest_handwritten_missing_history_report(env):
    config, _, deps, _, row = env
    rejected = _call(deps, "delivery_update", {"expected_revision": row["delivery_revision"],
        "stages": [{"id": "invented", "state": "completed"}]})
    candidate = rejected.error.recovery_hint["artifact_examples"][0]
    report = "lp_candidate_report.md"
    (config.paths.root / report).write_text("Pool history unavailable. No replay performed.\n")
    updated = tool_json_data(_call(deps, "delivery_update", {
        "expected_revision": row["delivery_revision"], "artifacts": [candidate,
            {"artifact_id": "report", "ref": report}], "limitations": ["Missing pool history"]}))
    final = _call(deps, "delivery_finalize", {"expected_revision": updated["delivery_revision"]})
    data = tool_json_data(final)
    reasons = data["completion"]["reasons"]
    assert "artifact_unverified:candidate" not in reasons
    assert "artifact_missing:candidate" not in reasons
    assert "artifact_unverified:report" in reasons
    assert "artifact_missing:run_receipt" in reasons
    assert "check_missing:report_output" in reasons
    assert "check_missing:historical_run" in reasons
    assert data["ok"] is False and data["completion"]["allowed"] is False
    assert not any(c["check_id"] == "report_output" for c in data["checks"])
    _, text = _render(final, config.paths.root)
    assert "handwritten" in text and "backtest report" in text
    assert '"artifact_id": "report"' in text
    assert candidate["ref"] in text
    assert '"expected_revision"' in text


def test_conflict_reports_current_cas_without_mutation(env):
    _, _, deps, _, row = env
    updated = tool_json_data(_call(deps, "delivery_update", {
        "expected_revision": row["delivery_revision"], "limitations": ["No history"]}))
    bad = _call(deps, "delivery_update", {"expected_revision": row["delivery_revision"], "artifacts": []})
    assert bad.is_error and "delivery_revision_conflict" in bad.error.message
    _, text = _render(bad)
    assert bad.error.recovery_hint["expected_revision"] == updated["delivery_revision"]
    assert f'"expected_revision":{updated["delivery_revision"]}' in text
    assert _read(env) == updated


def test_guidance_is_bounded_and_does_not_echo_model_paths_or_labels(env):
    _, _, deps, _, row = env
    marker = "UNTRUSTED_INSTRUCTION_" * 90
    updated = tool_json_data(_call(deps, "delivery_update", {
        "expected_revision": row["delivery_revision"], "artifacts": [{
            "artifact_id": "report", "ref": marker, "label": marker}]}))
    bad = _call(deps, "delivery_update", {"expected_revision": updated["delivery_revision"],
        "checks": [{"check_id": "report_output", "status": "passed", "message": marker}]})
    _, text = _render(bad)
    assert "UNTRUSTED_INSTRUCTION" not in text
    assert len(text) < 5000
    assert _read(env) == updated


def test_first_start_error_still_explains_artifact_shape(tmp_path):
    config, runtime = _runtime(tmp_path)
    deps = _deps(config, _command(runtime))
    bad = _call(deps, "delivery_start", {"skill_id": "strategy_author"})
    _, text = _render(bad)
    assert bad.is_error
    assert '"artifact_id":"candidate"' in text
    assert "latest delivery_revision" in text
    assert "expected_revision" not in bad.error.recovery_hint


def test_source_edit_stays_stale_and_is_not_offered_as_current_evidence(env):
    config, _, deps, binding, row = env
    candidate = {"artifact_id": "candidate", "ref": binding["artifact_id"], "revision": binding["revision"]}
    updated = tool_json_data(_call(deps, "delivery_update", {
        "expected_revision": row["delivery_revision"], "artifacts": [candidate]}))
    main = next(config.paths.root.glob("evolution/proposals/*/after/strategies/lp_candidate/main.py"))
    main.write_text("def run(ctx): return ctx.result.hold(reason='edited')\n")
    final = tool_json_data(_call(deps, "delivery_finalize", {"expected_revision": updated["delivery_revision"]}))
    source = next(c for c in final["checks"] if c["check_id"] == "source_binding")
    assert source["status"] == "stale"
    assert "artifact_unverified:candidate" in final["completion"]["reasons"]
    assert final["completion"]["allowed"] is False
    assert final["delivery_guidance"]["artifact_examples"][0]["ref"] == "<source_binding.artifact_id>"
