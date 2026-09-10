"""Reflection collects evidence; durable learning requires a separate justified write."""
from __future__ import annotations

import hashlib
import json

import pytest

from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.evolution.reflection_engine import run_reflection
from nerya.memory.runtime import MemoryRuntime

pytestmark = pytest.mark.smoke


def test_reflection_snapshot_is_reproducible_without_automatic_memory(tmp_path):
    config = Config(paths=WorkspacePaths(tmp_path), data={})
    first = run_reflection(config.paths, strategy_ids=[], config=config)
    second = run_reflection(config.paths, strategy_ids=[], config=config)
    assert first["ok"] and not first["has_evidence"]
    assert first["evidence_sha256"] == second["evidence_sha256"]
    blob = (tmp_path / first["snapshot_ref"].removeprefix("file:")).read_bytes()
    assert hashlib.sha256(blob).hexdigest() == first["evidence_sha256"]
    assert json.loads(blob)["strategies"] == {}
    assert MemoryRuntime(config).recall("Reflection scan errors") == []


@pytest.mark.parametrize("strategy_id", ["does-not-exist", "../escape", "/tmp"])
def test_reflection_rejects_invalid_strategy_without_creating_it(tmp_path, strategy_id):
    config = Config(paths=WorkspacePaths(tmp_path), data={})
    result = run_reflection(config.paths, strategy_ids=[strategy_id], config=config)
    assert result["strategies"] == {}
    assert result["invalid_strategy_ids"] == [strategy_id]
    assert not result["ok"]
    assert not config.paths.strategies.exists()


def test_disabling_learning_does_not_prevent_observation_collection(tmp_path):
    config = Config(paths=WorkspacePaths(tmp_path), data={
        "memory": {"write_rules": {"learning": {"enabled": False}}},
    })
    result = run_reflection(config.paths, strategy_ids=[], config=config)
    assert result["ok"]
    assert not config.paths.memory.exists()
