from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nerya.core.config import Config
from nerya.core.errors import LLMError
from nerya.core.paths import WorkspacePaths
from nerya.llm.gateway import LLMCall
from nerya.subagents.registry import SubAgentExecutionPolicy, SubAgentSpec
from nerya.subagents.runtime import (
    EXPLICIT_PAYLOAD_ONLY_CONTEXT_SCOPE,
    SubAgentLLMError,
    SubAgentRuntime,
)


pytestmark = pytest.mark.smoke


class _TransientLegacyGateway:
    def __init__(self, *, always_fail: bool = False) -> None:
        self.always_fail = always_fail
        self.calls = 0

    def call(self, **kwargs: Any) -> LLMCall:
        self.calls += 1
        if self.always_fail or self.calls == 1:
            raise LLMError("openai messages api error (503): unavailable")
        parsed = {"done": True, "summary": "evidence collected"}
        return LLMCall(
            tier=str(kwargs.get("tier") or "light"),
            task=str(kwargs.get("task") or "subagent_analysis"),
            caller=str(kwargs.get("caller") or "subagent:budget_child"),
            tokens=7,
            usd=0.01,
            raw=json.dumps(parsed),
            parsed=parsed,
            provider="fixture",
            model="fixture",
        )


class _EmptyRegistry:
    def list(self) -> list[Any]:
        return []


def _runtime(
    tmp_path: Path,
    gateway: _TransientLegacyGateway,
    *,
    extra_attempts: int,
) -> SubAgentRuntime:
    return SubAgentRuntime(
        config=Config(
            paths=WorkspacePaths(root=tmp_path),
            data={
                "agent": {
                    "subagents": {
                        "max_extra_llm_attempts_per_run": extra_attempts,
                    }
                }
            },
        ),
        skills=SimpleNamespace(registry=_EmptyRegistry()),  # type: ignore[arg-type]
        llm=gateway,  # type: ignore[arg-type]
    )


def _spec(tmp_path: Path) -> SubAgentSpec:
    return SubAgentSpec(
        name="budget_child",
        prompt_path=tmp_path / "budget_child.agent.md",
        prompt="Return a structured final answer.",
        tier="light",
        execution_policy=SubAgentExecutionPolicy(
            max_iterations=1,
            max_skill_calls=0,
            max_wall_seconds=30.0,
            llm_max_attempts=5,
            runtime="legacy",
        ),
    )


def test_legacy_subagent_transient_retry_consumes_one_shared_attempt(
    tmp_path: Path,
) -> None:
    gateway = _TransientLegacyGateway()
    runtime = _runtime(tmp_path, gateway, extra_attempts=1)

    result = runtime.run(
        _spec(tmp_path),
        trigger_event_id="trigger-1",
        payload={"task": "collect evidence"},
        session_id="session-1",
        turn_id="turn-1",
        context_scope=EXPLICIT_PAYLOAD_ONLY_CONTEXT_SCOPE,
        runtime_mode="legacy",
    )

    assert gateway.calls == 2
    assert result["output"]["summary"] == "evidence collected"
    assert result["metrics"]["attempt_budget"] == {
        "limit": 1,
        "used": 1,
        "remaining": 0,
        "by_reason": {"transient_retry": 1},
        "denied": 0,
    }
    assert [
        step["kind"]
        for step in result["steps"]
        if step["kind"] == "think_retry"
    ] == ["think_retry"]


def test_legacy_subagent_zero_attempt_budget_never_retries(
    tmp_path: Path,
) -> None:
    gateway = _TransientLegacyGateway(always_fail=True)
    runtime = _runtime(tmp_path, gateway, extra_attempts=0)

    with pytest.raises(SubAgentLLMError, match="failed before producing output"):
        runtime.run(
            _spec(tmp_path),
            trigger_event_id="trigger-1",
            payload={"task": "collect evidence"},
            session_id="session-1",
            turn_id="turn-1",
            context_scope=EXPLICIT_PAYLOAD_ONLY_CONTEXT_SCOPE,
            runtime_mode="legacy",
        )

    assert gateway.calls == 1
