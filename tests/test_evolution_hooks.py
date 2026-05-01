from __future__ import annotations

from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.evolution.event_store import list_events, list_signals
from nerya.evolution.hooks import EvolutionHookBus
import pytest

pytestmark = pytest.mark.smoke


def test_hook_bus_records_tool_failure(tmp_path):
    cfg = Config(paths=WorkspacePaths(tmp_path), data={})
    bus = EvolutionHookBus(cfg)

    bus.after_tool_result(
        turn_id="trn_1",
        tool="read_file",
        ok=False,
        error="boom",
    )

    rows = list_signals(cfg.paths)
    assert rows
    assert rows[0]["kind"] == "tool_failure_cluster"


def test_hook_bus_records_session_end(tmp_path):
    cfg = Config(paths=WorkspacePaths(tmp_path), data={})
    bus = EvolutionHookBus(cfg)

    bus.on_session_end(session_id="ses_1", report={"reason": "done"})

    rows = list_events(cfg.paths)
    assert rows[0]["evidence_refs"] == ["session:ses_1"]
