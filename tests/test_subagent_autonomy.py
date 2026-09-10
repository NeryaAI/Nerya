"""Autonomy is caller-owned policy, not hidden defaults or name-based routing."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from nerya.agent.kernel import _loop_config_from_config
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.subagents.registry import SubAgentExecutionPolicy, SubAgentSpec
from nerya.subagents.runtime import SubAgentRuntime

pytestmark = pytest.mark.smoke


def _runtime(tmp_path, settings=None):
    return SubAgentRuntime(
        config=Config(paths=WorkspacePaths(tmp_path), data=settings or {}),
        skills=SimpleNamespace(), llm=SimpleNamespace(), tool_executor=object(),
        tool_registry=SimpleNamespace(list_tools=lambda: [
            SimpleNamespace(name=name, risk=SimpleNamespace(value="read"), child_max_depth=None)
            for name in ("alpha", "beta", "gamma")
        ]),
    )


def test_disjoint_allowlists_stay_empty_after_merge_and_roundtrip(tmp_path):
    base = SubAgentExecutionPolicy(native_tool_allow=["alpha"])
    merged = base.merged({"native_tools": {"allow": ["beta"]}})
    restored = SubAgentExecutionPolicy.from_dict(merged.asdict())
    spec = SubAgentSpec(name="custom", prompt_path=tmp_path / "role.md", execution_policy=restored)
    assert _runtime(tmp_path)._allowed_native_tool_names(spec=spec) == []
    assert restored.merged({}).native_tool_allow == []


def test_unset_and_explicit_empty_allowlists_are_different(tmp_path):
    runtime = _runtime(tmp_path)
    unrestricted = SubAgentSpec(name="custom", prompt_path=tmp_path / "role.md")
    empty = SubAgentSpec(name="custom", prompt_path=tmp_path / "role.md", execution_policy={"native_tools": {"allow": []}})
    assert runtime._allowed_native_tool_names(spec=unrestricted) == ["alpha", "beta", "gamma"]
    assert runtime._allowed_native_tool_names(spec=empty) == []


def test_zero_call_budget_survives_parse_merge_and_roundtrip(tmp_path):
    policy = SubAgentExecutionPolicy.from_dict({"max_skill_calls": 0})
    merged = policy.merged({"max_skill_calls": 50})
    restored = SubAgentExecutionPolicy.from_dict(merged.asdict())
    spec = SubAgentSpec(name="custom", prompt_path=tmp_path / "role.md", execution_policy=restored)
    assert restored.max_skill_calls == 0
    assert _runtime(tmp_path)._max_skill_calls(spec) == 0


@pytest.mark.parametrize("wall", [0.0, 0.25, 2.0])
def test_child_budget_is_not_silently_raised_to_five_seconds(tmp_path, wall):
    spec = SubAgentSpec(name="custom", prompt_path=tmp_path / "role.md", execution_policy={"max_wall_seconds": wall})
    assert _runtime(tmp_path)._max_wall_seconds(spec) == wall


def test_zero_finalization_reserve_is_an_opt_out(tmp_path):
    runtime = _runtime(tmp_path, {"agent": {"subagents": {"finalization_reserve_seconds": 0}}})
    assert runtime._finalization_reserve_seconds() == 0


@pytest.mark.parametrize("field,value", [
    ("temperature", 0.75), ("repeated_tool_window", 9),
    ("repeated_tool_threshold", 6), ("repeated_tool_stop_after", 4),
    ("enable_microcompact", False), ("microcompact_max_chars", 3210),
    ("microcompact_keep_recent", 1), ("diminishing_returns_window", 8),
    ("diminishing_returns_threshold", 222),
])
def test_kernel_forwards_existing_loop_policy_settings(tmp_path, field, value):
    cfg = Config(paths=WorkspacePaths(tmp_path), data={"agent": {"native": {field: value}}})
    assert getattr(_loop_config_from_config(cfg), field) == value


@pytest.mark.parametrize("value", [-1, float("inf"), float("nan"), "bad"])
def test_invalid_child_limit_is_not_silently_unbounded(value):
    with pytest.raises(ValueError, match="max_wall_seconds"):
        SubAgentExecutionPolicy.from_dict({"max_wall_seconds": value})


def test_recovery_groups_turns_from_one_journal_read(tmp_path, monkeypatch):
    from nerya.agent import recovery
    paths = WorkspacePaths(tmp_path)
    journal = paths.journal("turn_steps")
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.touch()
    rows = [{"turn_id": f"turn-{i}", "index": 1, "step_kind": "observe", "status": "ok", "tokens": i} for i in range(8)]
    rows.append({"turn_id": "closed", "index": 2, "step_kind": "close"})
    reads = []
    monkeypatch.setattr(recovery.jsonl, "read_all", lambda path: reads.append(path) or list(rows))
    states = recovery.list_open_turns(paths)
    assert len(reads) == 1
    assert [state.turn_id for state in states] == [f"turn-{i}" for i in range(8)]
    assert sum(state.tokens for state in states) == 28
