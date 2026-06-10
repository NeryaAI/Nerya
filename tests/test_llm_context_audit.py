from __future__ import annotations

from pathlib import Path

import pytest

from nerya.core import jsonl
from nerya.llm.context_audit import build_context_full_audit


pytestmark = pytest.mark.smoke


def test_context_full_audit_groups_calls_and_keeps_payload_on_request(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "llm_context_full.jsonl"
    jsonl.append_many(
        log_path,
        [
            {
                "kind": "llm.context_full",
                "api": "messages",
                "phase": "request",
                "call_id": "call-main",
                "ts": "2026-06-08T01:00:00Z",
                "caller": "agent.loop",
                "task": "agent.loop",
                "tier": "medium",
                "provider": "minimax-cn",
                "model": "MiniMax-M3",
                "session_id": "sess-1",
                "turn_id": "turn-1",
                "iteration": 2,
                "context_scope": "agent_loop",
                "team_run_id": "team-1",
                "tools_sent_count": 2,
                "request": {
                    "system": "system text",
                    "messages": [{"role": "user", "content": "full prompt"}],
                    "tools": [{"name": "team_run"}, {"name": "web_search"}],
                    "metadata": {"turn_id": "turn-1"},
                },
            },
            {
                "kind": "llm.context_full",
                "api": "messages",
                "phase": "wire_request",
                "call_id": "call-main",
                "ts": "2026-06-08T01:00:01Z",
                "session_id": "sess-1",
                "turn_id": "turn-1",
                "iteration": 2,
                "team_run_id": "team-1",
                "wire_attempt": 1,
                "request": {
                    "method": "POST",
                    "url": "https://api.minimaxi.com/v1/chat/completions",
                    "body": {"model": "MiniMax-M3", "messages": []},
                },
            },
            {
                "kind": "llm.context_full",
                "api": "messages",
                "phase": "wire_response",
                "call_id": "call-main",
                "ts": "2026-06-08T01:00:02Z",
                "session_id": "sess-1",
                "turn_id": "turn-1",
                "iteration": 2,
                "team_run_id": "team-1",
                "wire_attempt": 1,
                "response": {
                    "status": 200,
                    "body": {"choices": [{"message": {"content": "ok"}}]},
                },
            },
            {
                "kind": "llm.context_full",
                "api": "prompt",
                "phase": "error",
                "call_id": "call-sub",
                "ts": "2026-06-08T01:00:03Z",
                "caller": "subagent:sec_filing_reviewer",
                "task": "subagent_analysis",
                "tier": "medium",
                "provider": "minimax-cn",
                "model": "MiniMax-M3",
                "session_id": "sess-1",
                "turn_id": "turn-1",
                "iteration": 4,
                "context_scope": "subagent",
                "subagent": "sec_filing_reviewer",
                "parent_call_id": "toolu-team",
                "error": {"type": "LLMError", "message": "timeout"},
            },
        ],
        stamp=False,
    )

    audit = build_context_full_audit(log_path, include_payload=True)

    assert audit["path"] == str(log_path)
    assert audit["total_records"] == 4
    assert audit["call_count"] == 2
    assert audit["summary"] == {
        "requests": 1,
        "responses": 0,
        "errors": 1,
        "wire_requests": 1,
        "wire_responses": 1,
        "wire_errors": 0,
    }
    assert audit["by_scope"] == {"agent_loop": 1, "subagent": 1}
    assert audit["by_subagent"] == {"sec_filing_reviewer": 1}

    first = audit["calls"][0]
    assert first["call_id"] == "call-main"
    assert first["session_id"] == "sess-1"
    assert first["turn_id"] == "turn-1"
    assert first["iteration"] == 2
    assert first["context_scope"] == "agent_loop"
    assert first["team_run_id"] == "team-1"
    assert first["request_shape"] == {
        "system_chars": 11,
        "message_count": 1,
        "message_chars": 11,
        "tool_count": 2,
        "tool_names": ["team_run", "web_search"],
    }
    assert first["wire"][0]["method"] == "POST"
    wire_response = next(item for item in first["wire"] if item["phase"] == "wire_response")
    assert wire_response["status"] == 200
    assert first["request"]["messages"][0]["content"] == "full prompt"

    filtered = build_context_full_audit(log_path, team_run_id="team-1")
    assert filtered["call_count"] == 1
    assert filtered["calls"][0]["call_id"] == "call-main"

    second = audit["calls"][1]
    assert second["call_id"] == "call-sub"
    assert second["error"] == {"type": "LLMError", "message": "timeout"}
    assert second["parent_call_id"] == "toolu-team"


def test_context_full_audit_can_filter_by_turn_and_omit_payload(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "llm_context_full.jsonl"
    jsonl.append_many(
        log_path,
        [
            {
                "kind": "llm.context_full",
                "api": "messages",
                "phase": "request",
                "call_id": "call-a",
                "turn_id": "turn-a",
                "request": {"messages": [{"role": "user", "content": "a"}]},
            },
            {
                "kind": "llm.context_full",
                "api": "messages",
                "phase": "request",
                "call_id": "call-b",
                "turn_id": "turn-b",
                "request": {"messages": [{"role": "user", "content": "b"}]},
            },
        ],
        stamp=False,
    )

    audit = build_context_full_audit(log_path, turn_id="turn-b")

    assert audit["total_records"] == 1
    assert [call["call_id"] for call in audit["calls"]] == ["call-b"]
    assert "request" not in audit["calls"][0]
    assert audit["calls"][0]["request_shape"]["message_chars"] == 1


def test_context_full_audit_marks_pending_wire_calls(tmp_path: Path) -> None:
    log_path = tmp_path / "llm_context_full.jsonl"
    jsonl.append_many(
        log_path,
        [
            {
                "kind": "llm.context_full",
                "api": "messages",
                "phase": "request",
                "call_id": "call-pending",
                "ts": "2026-06-08T01:00:00Z",
                "context_scope": "team_final_synthesis",
                "request": {
                    "system": "sys",
                    "messages": [{"role": "user", "content": "evidence"}],
                    "tools": [],
                },
            },
            {
                "kind": "llm.context_full",
                "api": "messages",
                "phase": "wire_request",
                "call_id": "call-pending",
                "ts": "2026-06-08T01:00:01Z",
                "wire_attempt": 1,
                "request": {"method": "POST", "url": "https://example.test", "timeout": 45.0},
            },
            {
                "kind": "llm.context_full",
                "api": "messages",
                "phase": "wire_error",
                "call_id": "call-pending",
                "ts": "2026-06-08T01:00:46Z",
                "wire_attempt": 1,
                "response": {
                    "method": "POST",
                    "url": "https://example.test",
                    "elapsed_ms": 45000,
                    "error": {"type": "LLMError", "message": "network timeout"},
                },
            },
            {
                "kind": "llm.context_full",
                "api": "messages",
                "phase": "wire_request",
                "call_id": "call-pending",
                "ts": "2026-06-08T01:00:47Z",
                "wire_attempt": 2,
                "request": {"method": "POST", "url": "https://example.test", "timeout": 45.0},
            },
        ],
        stamp=False,
    )

    audit = build_context_full_audit(log_path)

    call = audit["calls"][0]
    assert audit["pending_call_count"] == 1
    assert call["status"] == "pending"
    assert call["duration_ms"] == 47000
    assert call["pending"] is True
    assert call["last_wire_phase"] == "wire_request"
    assert call["last_wire_attempt"] == 2
    assert call["wire_error_count"] == 1
    assert call["wire_error_summary"] == [
        {"wire_attempt": 1, "type": "LLMError", "message": "network timeout"}
    ]


def test_context_full_audit_summarises_provider_wire_payload_shape(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "llm_context_full.jsonl"
    jsonl.append_many(
        log_path,
        [
            {
                "kind": "llm.context_full",
                "api": "messages",
                "phase": "request",
                "call_id": "call-wire-shape",
                "ts": "2026-06-08T01:00:00Z",
                "request": {
                    "messages": [{"role": "user", "content": "canonical"}],
                    "tools": [{"name": "team_run"}],
                    "max_tokens": 8192,
                },
            },
            {
                "kind": "llm.context_full",
                "api": "messages",
                "phase": "wire_request",
                "call_id": "call-wire-shape",
                "ts": "2026-06-08T01:00:01Z",
                "wire_attempt": 1,
                "request": {
                    "method": "POST",
                    "url": "https://api.minimaxi.com/v1/chat/completions",
                    "body": {
                        "model": "MiniMax-M3",
                        "messages": [
                            {"role": "system", "content": "short system"},
                            {"role": "user", "content": "emit team"},
                        ],
                        "tools": [
                            {
                                "type": "function",
                                "function": {"name": "team_run"},
                            }
                        ],
                        "max_completion_tokens": 8192,
                        "temperature": 0.0,
                        "thinking": {"type": "disabled"},
                    },
                },
            },
            {
                "kind": "llm.context_full",
                "api": "messages",
                "phase": "wire_response",
                "call_id": "call-wire-shape",
                "ts": "2026-06-08T01:00:02Z",
                "wire_attempt": 1,
                "response": {
                    "method": "POST",
                    "url": "https://api.minimaxi.com/v1/chat/completions",
                    "elapsed_ms": 1200,
                    "status": 200,
                    "body": {
                        "choices": [
                            {
                                "finish_reason": "tool_calls",
                                "message": {
                                    "tool_calls": [
                                        {
                                            "type": "function",
                                            "function": {
                                                "name": "team_run",
                                                "arguments": "{}",
                                            },
                                        }
                                    ]
                                },
                            }
                        ]
                    },
                },
            },
        ],
        stamp=False,
    )

    audit = build_context_full_audit(log_path)

    call = audit["calls"][0]
    assert call["wire_request_shape"] == {
        "message_count": 2,
        "message_chars": 21,
        "tool_count": 1,
        "tool_names": ["team_run"],
        "max_tokens": 8192,
        "temperature": 0.0,
        "thinking": {"type": "disabled"},
    }
    assert call["wire_response_summary"] == {
        "status": 200,
        "finish_reason": "tool_calls",
        "tool_call_names": ["team_run"],
    }
