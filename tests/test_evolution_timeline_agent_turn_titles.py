from __future__ import annotations

from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.evolution.event_store import record_event
from nerya.evolution.timeline import build_timeline


def test_agent_turn_events_are_labeled_as_agent_turns(tmp_path):
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data={})
    record_event(
        cfg.paths,
        outcome="candidate",
        validation_status="not_run",
        strategy_id=None,
        summary="Agent turn trn_web completed.",
        evidence_refs=["turn:trn_web"],
        metadata={"scope": "agent_turn", "session_id": "sess_web"},
    )

    item = build_timeline(cfg, limit=10)["timeline"][0]

    assert item["title"] == "Agent turn completed"
    assert item["summary"] == "Agent turn trn_web completed."
    assert item["strategy_id"] is None
