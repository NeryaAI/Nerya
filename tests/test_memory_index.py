"""Canonical memory revision history, without a second JSONL index."""
import pytest

from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.memory.runtime import MemoryRuntime

pytestmark = pytest.mark.smoke


def test_remember_supersedes_the_previous_value(tmp_path):
    memory = MemoryRuntime(Config(paths=WorkspacePaths(tmp_path), data={}))
    old = memory.remember(category="preference", key="risk.max_leverage", content="3x").record
    latest = memory.remember(category="preference", key="risk.max_leverage", content="2x").record
    assert [record.memory_id for record in memory.recall("risk.max_leverage")] == [latest.memory_id]
    revisions = {record.memory_id: record for record in memory.store.projection_records(actor_id="default")}
    assert revisions[old.memory_id].status == "superseded"
    assert revisions[latest.memory_id].content == "2x"
    assert not memory.config.paths.memory_index.exists()
