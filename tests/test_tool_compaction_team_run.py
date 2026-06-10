from __future__ import annotations

import json

import pytest

from nerya.llm import tool_compaction as tc


pytestmark = pytest.mark.smoke


def _team_run_payload() -> dict[str, object]:
    return {
        "ok": True,
        "status": "completed",
        "team_run_id": "team-tsla",
        "team_template": "market_analysis_team",
        "task": "Analyze TSLA and produce a buy/hold/sell rating",
        "output_language": "zh",
        "analysis_language": "en",
        "roles_requested": ["research_manager", "risk_critic"],
        "roles_succeeded": ["research_manager", "risk_critic"],
        "roles_failed": [],
        "tokens_total": 12345,
        "usd_total": 0.0123,
        "results": [
            {
                "subagent": "research_manager",
                "ok": True,
                "tokens": 6400,
                "usd": 0.0064,
                "output": {
                    "rating": "Hold",
                    "thesis": "TSLA has balanced upside catalysts and valuation risk.",
                    "position_guidance": {
                        "size_range": "60-100% of benchmark",
                        "horizon": "12 months",
                    },
                    "confidence": 0.62,
                    "evidence": [
                        {"source": "market_data", "claim": "latest price available"}
                    ],
                },
            },
            {
                "subagent": "risk_critic",
                "ok": True,
                "tokens": 2200,
                "usd": 0.0022,
                "output": {
                    "verdict": "approve_with_reductions",
                    "recommended_size_pct": 0.03,
                    "reasons": ["single-name volatility remains high"],
                    "data_coverage": {
                        "tool_errors": [
                            {
                                "skill": "risk_check",
                                "error": "native tool returned is_error=true",
                            }
                        ]
                    },
                },
            },
        ],
        "failures": [],
        "aggregated": {
            "avg_confidence": 0.58,
            "summary": "x" * 6000,
        },
        "next_action": "Write the complete requested answer from results now.",
        "padding": "y" * 6000,
    }


def test_team_run_compaction_keeps_member_outputs_without_false_tool_error() -> None:
    result = tc.compact_tool_result("team_run", _team_run_payload(), size_threshold=0)

    assert not result.skipped
    assert result.rule_id == "team_run.summary"
    assert result.kept["team_run_id"] == "team-tsla"
    assert result.kept["team_template"] == "market_analysis_team"
    assert result.kept["output_language"] == "zh"
    assert result.kept["analysis_language"] == "en"
    assert result.kept["roles_succeeded"] == ["research_manager", "risk_critic"]
    assert result.kept["roles_failed"] == []
    assert result.kept["error"] is None
    assert "native tool returned is_error=true" not in result.summary

    outputs = result.kept["role_outputs"]
    manager = next(row for row in outputs if row["subagent"] == "research_manager")
    risk = next(row for row in outputs if row["subagent"] == "risk_critic")
    assert manager["output"]["rating"] == "Hold"
    assert "balanced upside" in manager["output"]["thesis"]
    assert risk["output"]["verdict"] == "approve_with_reductions"
    assert risk["output"]["recommended_size_pct"] == 0.03

    compacted = json.dumps(result.kept, ensure_ascii=False)
    assert "top_keys" not in compacted
    assert len(compacted) < 5000


def test_task_get_compaction_keeps_nested_team_summary() -> None:
    output = {
        "task_id": "team_run_id",
        "name": "team-tsla",
        "state": "succeeded",
        "team_summary": _team_run_payload(),
        "progress": ["started", "finished"],
        "padding": "z" * 6000,
    }

    result = tc.compact_tool_result("task_get", output, size_threshold=0)

    assert not result.skipped
    assert result.rule_id == "team_run.summary"
    assert result.kept["task_id"] == "team_run_id"
    assert result.kept["task_state"] == "succeeded"
    assert result.kept["team_run_id"] == "team-tsla"
    assert result.kept["role_outputs"][0]["subagent"] == "research_manager"
