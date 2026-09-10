"""Canonical storage, actor isolation and evidence-preserving context packing."""
from __future__ import annotations

import json

import pytest

from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.memory.runtime import MemoryRuntime

pytestmark = pytest.mark.smoke


def test_startup_neither_imports_nor_overwrites_unmanaged_memory_files(tmp_path):
    paths = WorkspacePaths(tmp_path)
    paths.memory.mkdir(parents=True)
    original = {
        paths.memory / "global.md": "# Personal notes\n\nOld custom fact.\n",
        paths.memory_index: json.dumps({"value": "Old custom fact.", "scope": "global"}) + "\n",
    }
    for path, text in original.items():
        path.write_text(text, encoding="utf-8")
    memory = MemoryRuntime(Config(paths=paths, data={}))
    assert memory.recall("Old custom fact") == []
    assert memory.remember(category="learning", content="New canonical finding.").ok
    assert memory.recall("canonical finding")[0].content == "New canonical finding."
    for path, text in original.items():
        assert path.read_text(encoding="utf-8") == text


def test_curated_notebook_never_crosses_actor_boundary(tmp_path):
    config = Config(paths=WorkspacePaths(tmp_path), data={})
    alice = MemoryRuntime(config, actor_id="alice")
    assert alice.remember(category="notebook_operator", content="Alice prefers weekly rebalancing.", key="horizon").ok
    assert "weekly rebalancing" in MemoryRuntime(config, actor_id="alice").context("horizon").stable
    assert "weekly rebalancing" not in MemoryRuntime(config, actor_id="bob").context("horizon").stable
    assert "weekly rebalancing" not in MemoryRuntime(config).context("horizon").stable


def test_context_skips_oversized_records_instead_of_hiding_usable_evidence(tmp_path):
    memory = MemoryRuntime(Config(paths=WorkspacePaths(tmp_path), data={}))
    memory.remember(category="learning", content="alpha " * 1000, importance=1.0,
                    source="oversized", key="alpha.large")
    short = memory.remember(category="learning", content="alpha measured result is 7.",
                            source="file:Reports/Alpha.json", key="alpha.small", importance=0.1).record
    context = memory.context("alpha", max_chars=500)
    assert "alpha measured result is 7." in context.dynamic
    assert "file:Reports/Alpha.json" in context.dynamic
    assert [record.memory_id for record in context.recalled] == [short.memory_id]
    assert len(context.stable) + len(context.dynamic) <= 500


def test_source_references_are_not_case_folded(tmp_path):
    memory = MemoryRuntime(Config(paths=WorkspacePaths(tmp_path), data={}))
    record = memory.remember(category="learning", content="alpha evidence",
                             evidence_refs=["file:Reports/Alpha.csv"], source="CaseSensitiveSource").record
    recalled = memory.recall("alpha")[0]
    assert recalled.memory_id == record.memory_id
    assert recalled.evidence_refs == ("file:Reports/Alpha.csv",)
    assert recalled.source_ref == "CaseSensitiveSource"


@pytest.mark.parametrize("enabled", [False, True])
def test_turn_memory_notifies_evolution_only_after_a_real_write(tmp_path, enabled):
    from types import SimpleNamespace
    from nerya.agent.kernel import AgentKernel
    config = Config(paths=WorkspacePaths(tmp_path), data={
        "agent": {"native": {"memory_write_on_turn": True}},
        "memory": {"write_rules": {"session_summary": {"enabled": enabled}}},
    })
    notifications = []
    kernel = AgentKernel.__new__(AgentKernel)
    kernel.config = config
    kernel._deps = None
    kernel._evolution_hooks = SimpleNamespace(on_memory_write=lambda **kwargs: notifications.append(kwargs))
    kernel._after_turn_memory(
        turn_id="turn-one", strategy_id=None, session_id="session-one",
        result=SimpleNamespace(final_text="Completed evidence collection.\nRemaining: reconcile order trace.",
                               actions=[], stopped_reason="end_turn"),
    )
    assert len(notifications) == int(enabled)
    if enabled:
        records = MemoryRuntime(config, session_id="session-one").recall("reconcile order trace")
        assert len(records) == 1
        assert "Remaining: reconcile order trace." in records[0].content
