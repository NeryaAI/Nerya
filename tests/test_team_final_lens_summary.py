"""Team evidence is a tool result, never a hard-coded root stopping condition."""
import pytest

from nerya.tools.result_contracts import TEAM_REPORT_RESULT_PROTOCOL
from nerya.tools.types import ToolResult
from test_agent_loop_final_summary import _Gateway, _descriptor, _loop, _response, _tool_use

pytestmark = pytest.mark.smoke


@pytest.mark.parametrize("name", ["team_run", "independent_review"])
def test_team_results_allow_followup_actions_and_preserve_every_view(name):
    payload = {
        "team_run_id": "team-evidence", "status": "completed", "ok": True,
        "results": [
            {"subagent": "valuation", "output": {"diagnosis": "outside competence",
             "decision_implication": "do not participate", "confidence": 0.92,
             "facts_used": [{"claim": "fixture observation 63,894", "source": "fixture-source"}]}},
            {"subagent": "risk", "output": {"diagnosis": "uncertainty remains",
             "decision_implication": "verify limits first", "confidence": 0.7}},
        ],
    }
    executed = []
    def team(call):
        executed.append(name)
        return ToolResult.from_json(tool_use_id=call.id, name=call.name, data=payload,
            result_protocol=TEAM_REPORT_RESULT_PROTOCOL, semantic_success=True)
    def verify(call):
        executed.append("verify_limits")
        return ToolResult.from_json(tool_use_id=call.id, name=call.name, data={"ok": True, "permitted": False})
    gateway = _Gateway(
        _response(_tool_use(name, call_id="research"), stop_reason="tool_use"),
        _response(_tool_use("verify_limits", call_id="verify"), stop_reason="tool_use"),
        _response({"type": "text", "text": "No order submitted: limits prohibit it."}),
    )
    outcome = _loop(gateway, [_descriptor(name, team), _descriptor("verify_limits", verify)]).run(
        system="Follow evidence and the operator's limits.", user_message="research and verify eligibility",
    )
    assert executed == [name, "verify_limits"]
    assert len(gateway.calls) == 3
    assert outcome.final_text == "No order submitted: limits prohibit it."
    evidence = str(gateway.calls[1]["messages"])
    for value in ("outside competence", "do not participate", "uncertainty remains",
                  "verify limits first", "0.92", "0.7", "63,894", "fixture-source"):
        assert value in evidence
    assert all(call["context_scope"] == "agent_loop" for call in outcome.model_calls)


def test_team_result_does_not_create_private_synthesis_methods():
    from nerya.agent.loop import WorkspaceNativeAgentLoop
    assert not hasattr(WorkspaceNativeAgentLoop, "_synthesize_team_run_final_answer")
    assert not hasattr(WorkspaceNativeAgentLoop, "_team_final_text_or_fallback")
