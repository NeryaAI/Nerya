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


def test_native_task_compaction_keeps_latest_recovery_and_producer_failure() -> None:
    noise = {f"history_{i}": {"rows": ["x" * 1000] * 20} for i in range(25)}
    next_call = {"tool": "producer_status", "arguments": {"job_id": "job-latest"}}
    failure = {
        **noise,
        "kind": "provider_unavailable",
        "message": "Producer stopped before covering the requested window",
        "retryable": True,
        "detail": {
            **noise,
            "provider": "archive",
            "action": "read",
            "reason": "request budget exhausted",
            "http_status": 429,
            "rpc_error_code": -32005,
            "rpc_method": "getState",
            "failure_stage": "history_read",
            "api_key": "private-test-key",
        },
    }
    for tool in ("task_get", "task_output"):
        for state, producer_status in (
            ("running", "running"),
            ("succeeded", "budget_exhausted"),
            ("failed", "interrupted"),
        ):
            producer = {
                **noise,
                "status": producer_status,
                "error": None,
                "error_kind": None,
                "job_id": "job-latest",
                "next_status_call": next_call,
                "completed_state_count": 7,
                "attempts_reserved": 9,
                "remaining_attempts": 0,
                "coverage_complete": False,
                "requested_window_complete": False,
                "performance_evidence": False,
                "receipt": {
                    **noise,
                    "missing_state_count": 3,
                    "matched_state_count": 7,
                    "failure": failure if state != "running" else None,
                },
                "failure": failure if state != "running" else None,
            }
            wrapper = {
                "task_id": "task-native",
                "state": state,
                "output": producer,
                "error": "worker stopped " + "e" * 6000 if state == "failed" else None,
                "error_kind": "worker_failure" if state == "failed" else None,
            }
            if tool == "task_get":
                wrapper.update({
                    "payload": {**noise, "job_id": "job-latest", "next_status_call": next_call},
                    "progress": [
                        {"ts": "old", "note": "old-progress", "payload": {
                            "status": "failed", "error": "obsolete-error",
                            "proposal_paths": ["unbounded-old-path" * 1000],
                        }}
                    ] * 30 + [{"ts": "latest", "note": "latest-progress", "payload": producer}],
                })
            if state == "running":
                wrapper.update({
                    "is_terminal": False,
                    "wait_status": "not_requested",
                    "next_required_action": "Call task_output with wait_for_completion=true",
                })

            result = tc.compact_tool_result(
                tool, wrapper, size_threshold=0, raw_ref="raw://2026-11-07/native-task-result",
            )
            assert not result.skipped
            kept = result.kept
            assert kept["raw_ref"] == "raw://2026-11-07/native-task-result"
            assert kept["task_id"] == "task-native"
            assert kept["state"] == state
            assert "status" not in kept  # Producer status is not the task's state.
            assert kept["error_kind"] == wrapper["error_kind"]
            assert kept["error"] is None if state != "failed" else len(kept["error"]) <= 800
            previews = [kept["output"]]
            if tool == "task_get":
                assert kept["payload"]["job_id"] == "job-latest"
                assert kept["payload"]["next_status_call"] == next_call
                assert kept["progress_count"] == 31
                assert len(kept["progress"]) == 1
                assert kept["progress"][0]["note"] == "latest-progress"
                previews.append(kept["progress"][0]["payload"])
            for preview in previews:
                assert preview["status"] == producer_status
                assert preview["job_id"] == "job-latest"
                assert preview["next_status_call"] == next_call
                for key in ("completed_state_count", "attempts_reserved", "remaining_attempts",
                            "coverage_complete", "requested_window_complete", "performance_evidence"):
                    assert preview[key] == producer[key]
                assert preview["receipt"]["missing_state_count"] == 3
                assert preview["receipt"]["matched_state_count"] == 7
                if state != "running":
                    for diagnostic in (preview["failure"], preview["receipt"]["failure"]):
                        assert diagnostic["kind"] == "provider_unavailable"
                        assert diagnostic["message"] == failure["message"]
                        assert diagnostic["retryable"] is True
                        assert diagnostic["detail"]["http_status"] == 429
                        assert diagnostic["detail"]["rpc_error_code"] == -32005
                        assert diagnostic["detail"]["rpc_method"] == "getState"
                        assert diagnostic["detail"]["failure_stage"] == "history_read"
            if state == "running":
                assert kept["is_terminal"] is False
                assert kept["wait_status"] == "not_requested"
                assert kept["next_required_action"] == wrapper["next_required_action"]
            else:
                assert "is_terminal" not in kept
            compacted = json.dumps(kept)
            assert "private-test-key" not in compacted
            assert "obsolete-error" not in compacted
            assert "unbounded-old-path" not in compacted
            assert len(compacted) < 12000

    for existing in ({}, {"action": "producer_recovery"}):
        table = {
            "provider": "archive", "action": "history", "kind": "table",
            "rows": [{"index": i, "value": i * 2} for i in range(180)],
            "row_count": 180,
        }
        if existing:
            table["next_required_action"] = existing
        for raw_ref in (None, "ephemeral-reference", "raw://2026-11-07/table-result"):
            result = tc.compact_tool_result("data_api", table, size_threshold=0, raw_ref=raw_ref)
            assert result.rule_id == "data_api.table"
            assert result.kept["row_count"] == 180
            assert result.kept["sample_truncated"] is True
            guidance = result.kept["next_required_action"]
            if existing:
                assert guidance["action"] == "producer_recovery"
                guidance = guidance["sample_guidance"]
            if raw_ref == "raw://2026-11-07/table-result":
                assert result.kept["raw_ref"] == raw_ref
                assert guidance["action"] == "recover_raw_output"
                assert guidance["raw_ref"] == raw_ref
                assert guidance["fallback"]["action"] == "narrow_supported_filters"
                assert "first" in guidance["hint"]
            else:
                assert "raw_ref" not in result.kept
                assert guidance["action"] == "narrow_supported_filters"
