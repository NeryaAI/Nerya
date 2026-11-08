from copy import deepcopy
import json

import pytest

from nerya.core import jsonl
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.llm.gateway import LLMGateway
from nerya.llm.messages import MessagesResponse

pytestmark = pytest.mark.smoke


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setenv("NERYA_CONTEXT_FULL_LOG", "0")
    monkeypatch.setenv("NERYA_LLM_CONTEXT_LOG", "0")
    data = deepcopy(DEFAULT_CONFIG)
    data["runtime"]["mock_mode"] = True
    data["llm"]["tiers"] = {
        "medium": {"provider": "mock", "model": "offline",
                   "allowed_tasks": ["agent.loop"]},
    }
    gateway = LLMGateway(Config(paths=WorkspacePaths(tmp_path), data=data))
    captured = []
    response = MessagesResponse(content=[{"type": "text", "text": "no tool calls"}])

    def backend(request):
        captured.append(request)
        return response

    monkeypatch.setattr(gateway, "_resolve_messages_backend", lambda *a, **kw: backend)

    def invoke(**kwargs):
        return gateway.call_messages(
            task="agent.loop", caller="test", tier="medium",
            system="PRIVATE SYSTEM",
            messages=[{"role": "user", "content": "PRIVATE MESSAGE"}], **kwargs,
        )

    return gateway, captured, response, invoke


def diagnostic(gateway):
    rows = jsonl.read_all(gateway.usage.journal_path)
    assert sum(row["kind"] == "llm.call" for row in rows) == 1
    diagnostics = [row for row in rows if row["kind"] == "llm.messages_diagnostic"]
    assert len(diagnostics) == 1
    assert not gateway.config.paths.dev_log("llm_context_full").exists()
    return diagnostics[0]


def test_offered_tools_and_zero_response_tools_are_distinct(harness):
    gateway, captured, response, invoke = harness
    tools = [{"name": "Skill", "description": "PRIVATE DESCRIPTION",
              "input_schema": {"private": "PRIVATE SCHEMA"}},
             {"name": "read_file"}]
    choice = {"type": "auto", "private": "PRIVATE CHOICE"}
    metadata = {"session_id": "session-1", "turn_id": "turn-2", "iteration": 3,
                "tools_sent_count": 999, "completed_tool_names": ["fictional"],
                "private": "PRIVATE METADATA"}
    before = deepcopy((tools, choice, metadata))
    assert invoke(tools=tools, tool_choice=choice, metadata=metadata) is response
    assert len(captured) == 1
    assert captured[0].tools == tools
    assert captured[0].tool_choice == choice
    assert (tools, choice, metadata) == before
    row = diagnostic(gateway)
    assert {k: v for k, v in row.items() if k != "ts"} == {
        "kind": "llm.messages_diagnostic",
        "session_id": "session-1", "turn_id": "turn-2", "iteration": 3,
        "tools_count": len(captured[0].tools), "tool_names": ["Skill", "read_file"],
        "tool_choice": {"type": "auto"},
        "stop_reason": "end_turn", "response_tools_count": 0,
        "response_tool_names": [],
    }
    assert "PRIVATE" not in json.dumps(row)


@pytest.mark.parametrize(("choice", "expected"), [
    (None, {"type": "auto"}),
    ({"type": " NONE "}, {"type": "none"}),
    ({"type": "any"}, {"type": "required"}),
    ({"type": "required"}, {"type": "required"}),
    ({"type": "tool", "name": "Skill"}, {"type": "tool", "name": "Skill"}),
    ({"type": "function", "function": {"name": "Skill", "arguments": "PRIVATE ARGS"}},
     {"type": "tool", "name": "Skill"}),
    ({"type": "PRIVATE UNKNOWN TYPE", "private": "PRIVATE CHOICE"}, {"type": "unknown"}),
])
def test_tool_choice_is_normalized_without_changing_dispatch(harness, choice, expected):
    gateway, captured, _, invoke = harness
    invoke(tool_choice=choice)
    assert captured[0].tool_choice == choice
    row = diagnostic(gateway)
    assert row["tools_count"] == 0
    assert row["tool_names"] == []
    assert row["tool_choice"] == expected
    assert "PRIVATE" not in json.dumps(row)


def test_response_counts_actual_tool_use_blocks_and_preserves_duplicates(harness):
    gateway, _, response, invoke = harness
    response.stop_reason = "max_tokens"
    response.content = [
        {"type": "text", "text": "PRIVATE RESPONSE"},
        {"type": "tool_use", "name": "Skill", "id": "PRIVATE ID",
         "input": {"command": "PRIVATE ARGS"}},
        {"type": "tool_use", "name": "Skill", "input": {}},
        {"type": "tool_result", "content": "PRIVATE RESULT"},
    ]
    response.raw = {"api_key": "PRIVATE PROVIDER BODY"}
    invoke(tools=[{"name": "Skill"}])
    row = diagnostic(gateway)
    assert row["stop_reason"] == "max_tokens"
    assert row["response_tools_count"] == 2
    assert row["response_tool_names"] == ["Skill", "Skill"]
    assert "PRIVATE" not in json.dumps(row)


def test_diagnostics_bound_and_redact_all_untrusted_labels(harness):
    gateway, captured, response, invoke = harness
    secret = "sk-" + "secretvalue" * 30
    long_name = "tool_" + "x_" * 300
    names = [secret, long_name, "/private/workspace/config.yml", "bad\nname"]
    names += [f"tool_{i}" for i in range(100)]
    response.content = [{"type": "tool_use", "name": name,
                         "input": {"secret": secret}} for name in names]
    response.stop_reason = secret
    invoke(tools=[{"name": name} for name in names],
           tool_choice={"type": "tool", "name": secret},
           metadata={"session_id": secret, "turn_id": long_name, "iteration": 7,
                     "provider_config": {"secret": secret}})
    row = diagnostic(gateway)
    assert row["tools_count"] == len(captured[0].tools) == len(names)
    assert row["response_tools_count"] == len(names)
    for field in ("tool_names", "response_tool_names"):
        assert len(row[field]) == 32
        assert all(len(name) <= 128 for name in row[field])
        assert row[field][1] == long_name[:128]
    assert len(row["turn_id"]) <= 128
    serialized = json.dumps(row)
    assert secret not in serialized
    assert secret[:128] not in serialized  # redact before truncating
    assert "/private/" not in serialized
    assert "bad\\nname" not in serialized
    assert len(serialized) < 10000


def test_non_scalar_correlation_is_not_logged(harness):
    gateway, _, _, invoke = harness
    invoke(metadata={"session_id": ["PRIVATE"], "turn_id": {"path": "PRIVATE"},
                     "iteration": [1, 2], "max_iterations": 100})
    row = diagnostic(gateway)
    assert not {"session_id", "turn_id", "iteration", "max_iterations"} & row.keys()


def test_diagnostic_write_failure_does_not_retry_or_change_response(harness, monkeypatch):
    gateway, captured, response, invoke = harness
    append = jsonl.append
    attempted = []

    def fail_diagnostic(path, record, **kwargs):
        if record.get("kind") == "llm.messages_diagnostic":
            attempted.append(record)
            raise OSError("journal unavailable")
        return append(path, record, **kwargs)

    monkeypatch.setattr(jsonl, "append", fail_diagnostic)
    assert invoke() is response
    assert len(captured) == 1
    assert len(attempted) == 1
    assert any(row["kind"] == "llm.call" for row in jsonl.read_all(gateway.usage.journal_path))
