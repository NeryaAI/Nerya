"""Performance evidence is a strict producer boolean, independent of replay success."""
import json

import pytest

from nerya.strategies.tool_verification import attach_strategy_checks
from nerya.tools.types import ToolResult

pytestmark = pytest.mark.smoke


def _adapt(evidence):
    binding = {"artifact_id": "strategy:cex:proposal:candidate", "revision": "a" * 64}
    data = {"ok": True, "requested_window_complete": True,
            "replay": {"order_accounting_ok": True,
                       "mock_surface_calls": {"/private/raw-payload.json": 1}},
            "provenance": {"source_revision": binding["revision"],
                           "source_changed_during_run": False}, **evidence}
    return attach_strategy_checks(
        ToolResult.from_json(tool_use_id="replay", name="strategy_backtest", data=data),
        before=binding, after=binding, kind="backtest")


@pytest.mark.parametrize("evidence,expected", [
    ({"performance_evidence": False}, "missing"),
    ({}, "missing"),
    ({"performance_evidence": True}, "passed"),
    ({"performance_evidence": 1}, "missing"),
    ({"performance_evidence": "true"}, "missing"),
    ({"performance_evidence": None}, "missing"),
])
def test_adapter_performance_requires_literal_true(evidence, expected):
    result = _adapt(evidence)
    receipt = result.metadata["verification"]
    rows = {row["check_id"]: row for row in receipt["checks"]}
    performance = rows.pop("historical_performance")
    assert performance["status"] == expected
    assert performance["required"] is False
    assert performance["evidence_ref"] == ""
    assert performance["label"] and performance["message"]
    if expected == "missing":
        assert all(word in performance["message"].lower() for word in ("simulated", "stub", "input-only"))
    assert "/private/raw-payload.json" not in json.dumps(receipt)
    assert ("historical_agent_trading_performance" in receipt["unverified"]) == (expected == "missing")
    assert result.semantic_success is True
    # The same source revision changes only the optional check, never the required checks.
    baseline = _adapt({"performance_evidence": True}).metadata["verification"]
    assert (receipt["artifact_id"], receipt["revision"]) == (baseline["artifact_id"], baseline["revision"])
    assert rows == {row["check_id"]: row for row in baseline["checks"]
                    if row["check_id"] != "historical_performance"}
    assert {key for key, row in rows.items() if row["required"]} == {
        "source_binding", "historical_run", "report_output", "replay_source"}
