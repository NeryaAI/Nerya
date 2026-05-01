from __future__ import annotations

from nerya.core import jsonl
from nerya.core.paths import WorkspacePaths
from nerya.evolution.event_store import list_signals
from nerya.evolution.signals import collect_signals
import pytest

pytestmark = pytest.mark.smoke


def test_collect_signals_detects_operator_correction(tmp_path):
    paths = WorkspacePaths(tmp_path)
    jsonl.append(paths.journal("agent"), {
        "kind": "agent.turn.start",
        "turn_id": "trn_1",
        "user_text": "不是这个意思，需要重新分析",
    })

    rows = collect_signals(paths, persist=True)

    assert any(row["kind"] == "user_correction" for row in rows)
    assert list_signals(paths, kind="user_correction")


def test_collect_signals_detects_repeated_noop(tmp_path):
    paths = WorkspacePaths(tmp_path)
    for i in range(3):
        jsonl.append(paths.journal("agent"), {
            "kind": "agent.turn.end",
            "turn_id": f"trn_{i}",
            "tool_calls": 0,
            "final_text": "",
            "stop_reason": "empty",
        })

    rows = collect_signals(paths)

    assert len([row for row in rows if row["kind"] == "repeated_noop"]) == 1
