"""Native memory tools must obey the runtime's trusted turn scope."""

from __future__ import annotations

import pytest

from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.tools.types import ToolCall


pytestmark = pytest.mark.smoke


def test_native_memory_schema_exposes_trusted_session_scope():
    from nerya.tools.native.memory import MEMORY_REMEMBER_SCHEMA

    assert "session" in MEMORY_REMEMBER_SCHEMA["properties"]["scope"]["enum"]


def test_native_remember_rejects_a_forged_strategy_id(tmp_path):
    from nerya.memory.runtime import MemoryRuntime
    from nerya.tools.native.memory import memory_remember_handler

    runtime = MemoryRuntime(
        Config(paths=WorkspacePaths(root=tmp_path), data={}),
        actor_id="operator-1",
        session_id="session-1",
        strategy_id="alpha",
    )
    call = ToolCall(
        name="memory_remember",
        arguments={
            "scope": "strategy",
            "strategy_id": "beta",
            "note": "Beta 策略的杠杆上限应改成五倍。",
        },
    )

    result = memory_remember_handler(call, runtime=runtime)

    assert result.is_error
    assert result.error is not None
    assert "active strategy" in result.error.message
    assert runtime.recall("杠杆上限") == []


def test_native_remember_writes_only_to_the_trusted_active_strategy(tmp_path):
    from nerya.memory.runtime import MemoryRuntime
    from nerya.tools.native.memory import memory_remember_handler

    config = Config(paths=WorkspacePaths(root=tmp_path), data={})
    alpha = MemoryRuntime(
        config,
        actor_id="operator-1",
        session_id="session-1",
        strategy_id="alpha",
    )
    call = ToolCall(
        name="memory_remember",
        turn_id="turn-1",
        arguments={
            "scope": "strategy",
            "strategy_id": "alpha",
            "category": "decision",
            "key": "risk.max_leverage",
            "note": "在波动率高于历史九十分位时，Alpha 策略的杠杆上限固定为两倍。",
        },
    )

    result = memory_remember_handler(call, runtime=alpha)

    assert not result.is_error
    assert result.content[0].data["ok"] is True
    assert [hit.content for hit in alpha.recall("杠杆上限")] == [
        "在波动率高于历史九十分位时，Alpha 策略的杠杆上限固定为两倍。"
    ]
    operator_view = MemoryRuntime(config, actor_id="operator-1")
    assert operator_view.recall("杠杆上限") == []
