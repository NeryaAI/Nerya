"""Reflection writes durable findings through the canonical runtime."""

from __future__ import annotations

import pytest

from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths


pytestmark = pytest.mark.smoke


def test_reflection_global_summary_uses_runtime_activity_and_recall(tmp_path):
    from nerya.evolution.reflection_engine import run_reflection
    from nerya.memory.activity import MemoryActivityLog
    from nerya.memory.runtime import MemoryRuntime

    config = Config(paths=WorkspacePaths(root=tmp_path), data={})

    result = run_reflection(config.paths, strategy_ids=[], config=config)

    assert result["ok"] is True
    hits = MemoryRuntime(config).recall("Reflection scan errors", limit=5)
    assert any("Reflection scan" in hit.content for hit in hits)
    events = MemoryActivityLog(config=config).tail(limit=10)
    assert any(
        event["kind"] == "write_ok" and event["source"] == "reflection:global"
        for event in events
    )


def test_reflection_rejects_nonexistent_explicit_strategy_ids(tmp_path):
    from nerya.evolution.reflection_engine import run_reflection

    config = Config(paths=WorkspacePaths(root=tmp_path), data={})

    result = run_reflection(
        config.paths,
        strategy_ids=["does-not-exist"],
        config=config,
    )

    assert result["strategies"] == {}
    assert not (tmp_path / "strategies" / "does-not-exist").exists()


def test_reflection_reports_a_blocked_canonical_write(tmp_path):
    from nerya.evolution.reflection_engine import run_reflection

    config = Config(
        paths=WorkspacePaths(root=tmp_path),
        data={"memory": {"write_rules": {"learning": {"enabled": False}}}},
    )

    result = run_reflection(config.paths, strategy_ids=[], config=config)

    assert result["ok"] is False
    assert result["write_error"] == "disabled"
