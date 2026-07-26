from __future__ import annotations

import pytest

from nerya.agent.memory_index import MemoryIndex
from nerya.core import jsonl
from nerya.core.paths import WorkspacePaths


pytestmark = pytest.mark.smoke


def test_remember_supersedes_the_previous_value(tmp_path) -> None:
    index = MemoryIndex(WorkspacePaths(tmp_path))

    index.remember(key="risk.max_leverage", value="3x", ts="2026-01-01T00:00:00Z")
    latest = index.remember(
        key="risk.max_leverage",
        value="2x",
        ts="2026-01-02T00:00:00Z",
    )

    assert index.current() == [latest]
    rows = jsonl.read_all(index.paths.memory_index)
    assert rows[0]["superseded"] is True
    assert rows[1]["value"] == "2x"
