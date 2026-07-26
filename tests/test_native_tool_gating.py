"""Progressive native tool disclosure (nerya.tools.native.tool_surfaces).

Verifies that:
1. A small core stays visible while specialized families are hidden by
   default, yet remain registered (dispatchable).
2. ``skill_view`` reveals the owning skill's surface(s) for the session.
3. Reveals land in the shared ``LazyMcpState.described_namespaces`` set
   (so the kernel session cache persists them across turns).
4. ``apply_native_lazy_surfaces`` is idempotent.
5. The runtime.native_tool_gating flag opts the whole thing out.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nerya.agent.loop import LoopConfig, WorkspaceNativeAgentLoop
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.core import yaml_io
from nerya.llm.messages import MessagesResponse
from nerya.tools.executor import NativeToolExecutor
from nerya.tools.native import build_native_tool_deps, register_native_tools
from nerya.tools.native.tool_surfaces import (
    apply_native_lazy_surfaces,
    described_key,
    reveal_surfaces_for_skill,
)
from nerya.tools.orchestrator import ToolOrchestrator
from nerya.tools.permissions import (
    PermissionContext,
    PermissionEngine,
    PermissionMode,
)
from nerya.tools.registry import ToolRegistry
from nerya.tools.types import ToolCall
from nerya.skills.kernel import SkillKernel

pytestmark = pytest.mark.smoke

BUILTIN_ROOT = Path(__file__).resolve().parents[1] / "nerya" / "skills" / "builtin"

CORE_EXPECTED = (
    "read_file",
    "list_dir",
    "glob",
    "grep",
    "edit_file",
    "write_file",
    "run_shell",
    "skill_index",
    "skill_view",
    "script_inspect",
    "script_run",
    "todo_write",
    "memory_recall",
    "web_search",
    "market_data",
)

# Gated families that are always registered in this minimal setup
# (team_/task_ tools require a subagent/task runtime, so they are absent
# here — but their surfaces are still defined for the full runtime).
GATED_EXPECTED = (
    "strategy_draft_proposal",
    "strategy_backtest",
    "trade_intent_submit",
    "llm_complete",
    "web_fetch",
    "data_api",
)


def _registry(tmp_path: Path) -> ToolRegistry:
    paths = WorkspacePaths(root=tmp_path)
    registry = ToolRegistry()
    deps = build_native_tool_deps(
        workspace_root=tmp_path,
        skill_roots=[BUILTIN_ROOT],
        paths=paths,
        config=Config(paths=paths),
    )
    register_native_tools(registry, deps)
    return registry


def _visible_names(registry: ToolRegistry) -> set[str]:
    state = getattr(registry, "lazy_mcp_state", None)
    if state is None:
        return {t.name for t in registry.list_tools()}
    return {t.name for t in registry.list_tools() if state.is_visible(t)}


def test_core_visible_specialized_hidden_by_default(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    visible = _visible_names(registry)
    for name in CORE_EXPECTED:
        assert name in visible, f"core tool {name} should be visible"
    for name in GATED_EXPECTED:
        assert name not in visible, f"gated tool {name} should be hidden"
        # Still registered → dispatchable even while hidden from prompt.
        assert registry.find(name) is not None, f"{name} must stay registered"


def test_gating_shrinks_the_rendered_surface(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    total = len(registry.list_tools())
    visible = len(_visible_names(registry))
    # A meaningful reduction — well over a third of tools are gated.
    assert visible < total
    assert (total - visible) >= 30


def test_skill_index_reuses_active_registry_paths(tmp_path: Path) -> None:
    paths = WorkspacePaths(root=tmp_path)
    yaml_io.dump(
        paths.skills_enabled,
        {"version": 1, "enabled": ["research", "finance.private_equity.ic_memo"]},
    )
    config = Config(paths=paths)
    skills = SkillKernel.boot(config)
    deps = build_native_tool_deps(
        workspace_root=tmp_path,
        skill_roots=[BUILTIN_ROOT],
        paths=paths,
        config=config,
        skills=skills,
    )

    assert {record.skill_id for record in deps.skill_index.records()} == {
        "research",
        "finance.private_equity.ic_memo",
    }


def test_skill_view_reveals_owning_surface(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    assert "strategy_draft_proposal" not in _visible_names(registry)

    skill_view = registry.get("skill_view")
    result = skill_view.handler(
        ToolCall(name="skill_view", arguments={"skill_id": "strategy_author"})
    )
    assert not result.is_error

    visible = _visible_names(registry)
    # strategy_author unlocks strategy + trading + markets surfaces.
    assert "strategy_draft_proposal" in visible
    assert "strategy_backtest" in visible
    assert "trade_intent_submit" in visible
    assert "data_api" in visible
    # Unrelated surfaces stay hidden.
    assert "team_run" not in visible
    assert "task_create" not in visible


def test_reveal_lands_in_described_set(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    newly = reveal_surfaces_for_skill(registry, "research")
    assert "research" in newly
    state = registry.lazy_mcp_state
    assert described_key("research") in state.described_namespaces
    assert "web_fetch" in _visible_names(registry)
    # A second reveal is a no-op.
    assert reveal_surfaces_for_skill(registry, "research") == []


def test_apply_is_idempotent(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    before = {t.name: t.lazy for t in registry.list_tools()}
    apply_native_lazy_surfaces(registry)
    after = {t.name: t.lazy for t in registry.list_tools()}
    assert before == after
    desc = registry.get("strategy_backtest")
    assert sum(1 for tag in desc.tags if tag.startswith("surface:")) == 1


class _RevealThenAnswerGateway:
    """Iteration 1 loads a skill; iteration 2 finishes with text.

    Records the set of advertised tool names handed to the provider on
    every call so the test can assert that a surface unlocked mid-turn
    becomes visible *within the same turn* — i.e. on the iteration that
    immediately follows the reveal, not only on the next turn.

    ``tool_name`` / ``arg_key`` let the test exercise both the discovery
    ``skill_view(skill_id=...)`` tool and the canonical
    ``Skill``/``skill`` invoke tool (``{"skill": ...}``) that the system
    prompt actually instructs models to use.
    """

    def __init__(self, skill: str, *, tool_name: str, arg_key: str) -> None:
        self._skill = skill
        self._tool_name = tool_name
        self._arg_key = arg_key
        self.tools_per_call: list[set[str]] = []
        self._n = 0

    def call_messages(self, **kwargs):  # noqa: ANN003
        tools = kwargs.get("tools") or []
        self.tools_per_call.append(
            {str(t.get("name")) for t in tools if isinstance(t, dict) and t.get("name")}
        )
        self._n += 1
        if self._n == 1:
            return MessagesResponse(
                content=[
                    {
                        "type": "tool_use",
                        "id": "toolu_reveal",
                        "name": self._tool_name,
                        "input": {self._arg_key: self._skill},
                    }
                ],
                stop_reason="tool_use",
                usage={"input_tokens": 100, "output_tokens": 10},
            )
        return MessagesResponse(
            content=[{"type": "text", "text": "done"}],
            stop_reason="end_turn",
            usage={"input_tokens": 100, "output_tokens": 10},
        )


@pytest.mark.parametrize(
    ("tool_name", "arg_key"),
    [
        ("skill_view", "skill_id"),
        # The canonical playbook-invocation tool the system prompt tells
        # models to use. Regression: its handler did not fire the reveal,
        # so capable models (e.g. gpt-5.x) that correctly call ``Skill``
        # found the strategy tools "not exposed".
        ("Skill", "skill"),
        ("skill", "skill"),
    ],
)
def test_skill_load_unlocks_tools_within_the_same_turn(
    tmp_path: Path, tool_name: str, arg_key: str
) -> None:
    """Regression on two fronts that together broke the live flow:

    1. ``provider_tools`` was rendered once before the loop, so a reveal
       mid-turn did not reach the advertised tool list until the next
       turn (fixed by an in-loop re-render).
    2. Only ``skill_view`` fired the surface reveal; the canonical
       ``Skill``/``skill`` invoke tool did not (fixed by wrapping its
       handler).

    Both must hold for the unlocked tools to appear on the very next
    iteration of the same turn.
    """

    registry = _registry(tmp_path)
    executor = NativeToolExecutor(
        registry=registry,
        permission_engine=PermissionEngine(),
        permission_context=PermissionContext(mode=PermissionMode.AUTO),
    )
    orchestrator = ToolOrchestrator(registry=registry, executor=executor)
    gateway = _RevealThenAnswerGateway(
        "strategy_author", tool_name=tool_name, arg_key=arg_key
    )
    loop = WorkspaceNativeAgentLoop(
        gateway=gateway,  # type: ignore[arg-type]
        registry=registry,
        orchestrator=orchestrator,
        config=LoopConfig(max_iterations=4),
    )

    outcome = loop.run(system="system", user_message="build a strategy")

    assert not outcome.aborted
    assert len(gateway.tools_per_call) >= 2
    # Iteration 1: gated strategy tools hidden, skill-loading available.
    assert "strategy_draft_proposal" not in gateway.tools_per_call[0]
    # Iteration 2 (post-reveal): the unlocked surface is now advertised.
    assert "strategy_draft_proposal" in gateway.tools_per_call[1]
    assert "strategy_backtest" in gateway.tools_per_call[1]


def test_skill_invoke_tool_reveals_surface_at_registry_level(tmp_path: Path) -> None:
    """The ``Skill`` tool handler itself must fire the surface reveal."""

    registry = _registry(tmp_path)
    assert "strategy_draft_proposal" not in _visible_names(registry)

    skill_tool = registry.get("Skill")
    result = skill_tool.handler(
        ToolCall(name="Skill", arguments={"skill": "strategy_author"})
    )
    assert not result.is_error

    visible = _visible_names(registry)
    assert "strategy_draft_proposal" in visible
    assert "strategy_backtest" in visible


def test_gating_flag_off_keeps_tools_eager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NERYA_FF_RUNTIME_NATIVE_TOOL_GATING", "0")
    from nerya.runtime import feature_flags as ff

    ff.reset_cache()
    try:
        registry = _registry(tmp_path)
        # No tools marked lazy → everything advertised.
        assert registry.get("strategy_backtest").lazy is False
        assert registry.get("trade_intent_submit").lazy is False
    finally:
        monkeypatch.delenv("NERYA_FF_RUNTIME_NATIVE_TOOL_GATING", raising=False)
        ff.reset_cache()
