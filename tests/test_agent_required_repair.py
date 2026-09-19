"""Deterministic regression for a failure found by real authoring calls."""
import pytest
from nerya.agent.tool_phase import failed_required_actions
from nerya.tools.types import ToolResult, ToolError, ToolErrorKind
from test_agent_loop_final_summary import _Gateway, _response, _tool_use, _descriptor, _loop, _json_result

pytestmark = pytest.mark.smoke


def test_required_failure_allows_repair_then_rechecks_same_requirement():
    seen = []
    repaired = False
    def handler(call):
        nonlocal repaired
        seen.append(call.name)
        if call.name == "prepare":
            return _json_result(call, {"ok": True, "next_required_action": {"tool": "verify"}})
        if call.name == "repair":
            repaired = True
            return _json_result(call, {"ok": True})
        if not repaired:
            return ToolResult.from_error(tool_use_id=call.id, name=call.name,
                error=ToolError(kind=ToolErrorKind.EXECUTION_ERROR, message="candidate code is broken"))
        return _json_result(call, {"ok": True})
    gateway = _Gateway(
        _response(_tool_use("prepare", call_id="a")),
        _response(_tool_use("verify", call_id="b")),
        _response(_tool_use("repair", call_id="c")),
        _response(_tool_use("verify", call_id="d")),
        _response({"type": "text", "text": "Verified after repair"}),
    )
    loop = _loop(gateway, [_descriptor(name, handler) for name in ("prepare", "verify", "repair")], max_iterations=8)
    outcome = loop.run(system="Complete the requested work", user_message="Build and verify")
    assert seen == ["prepare", "verify", "repair", "verify"]
    assert outcome.final_text == "Verified after repair"
    def names(call):
        return {t.get("name") or t.get("function", {}).get("name") for t in call["tools"]}
    assert names(gateway.calls[1]) == {"verify"}
    assert names(gateway.calls[2]) == {"prepare", "verify", "repair"}
    assert "admin" not in names(gateway.calls[2])


@pytest.mark.parametrize("kind", [ToolErrorKind.PERMISSION_DENIED, ToolErrorKind.UNKNOWN])
def test_repair_does_not_treat_permission_or_unknown_errors_as_execution_failure(kind):
    result = ToolResult.from_error(tool_use_id="a", name="verify", error=ToolError(kind=kind, message="blocked"))
    assert failed_required_actions({"verify"}, [result]) == set()


def test_unknown_effect_and_newest_success_stay_closed():
    uncertain = ToolResult.from_error(tool_use_id="a", name="verify", error=ToolError(
        kind=ToolErrorKind.EXECUTION_ERROR, message="worker disappeared", detail={"execution_state": "unknown"}))
    assert failed_required_actions({"verify"}, [uncertain]) == set()
    failure = ToolResult.from_error(tool_use_id="b", name="verify", error=ToolError(
        kind=ToolErrorKind.EXECUTION_ERROR, message="failed"))
    success = ToolResult.from_json(tool_use_id="c", name="verify", data={"ok": True})
    assert failed_required_actions({"verify"}, [failure]) == {"verify"}
    assert failed_required_actions({"verify"}, [failure, success]) == set()
    assert failed_required_actions({"other"}, [failure]) == set()
