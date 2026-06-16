from __future__ import annotations

from datetime import datetime, timedelta, timezone

from nerya.agent import self_improvement
from nerya.agent.self_improvement import maybe_propose_from_turn
from nerya.core import jsonl
from nerya.core import time as time_core
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
import pytest

pytestmark = pytest.mark.smoke


def test_legacy_evolve_delegates_to_canonical_runner(tmp_path, monkeypatch):
    config = _config(tmp_path)
    sentinel = {
        "proposal": {"id": "prp_reflect_1", "validation_plan_id": "vpl_1"},
        "signals": [{"id": "sig_1"}],
        "selected_assets": {"genes": [{"id": "gene_1"}]},
        "event": {"id": "evt_1"},
    }

    def fake_evolve(received_config: Config) -> dict:
        assert received_config is config
        return sentinel

    monkeypatch.setattr("nerya.evolution.runner.evolve", fake_evolve)

    assert self_improvement.evolve(config) is sentinel


def _config(tmp_path, data: dict | None = None) -> Config:
    return Config(paths=WorkspacePaths(tmp_path), data=data or {})


def _append_noop_turns(config: Config, count: int = 10) -> None:
    for i in range(count):
        jsonl.append(config.paths.journal("agent"), {
            "kind": "agent.turn.end",
            "turn_id": f"trn_{i}",
            "action": "noop",
        })


def test_noop_auto_proposal_is_throttled_for_10_minutes(tmp_path):
    config = _config(tmp_path)
    started_at = datetime(2026, 5, 3, 1, 0, tzinfo=timezone.utc)
    time_core.set_clock(lambda: started_at)
    try:
        _append_noop_turns(config)

        first = maybe_propose_from_turn(config, turn={})
        assert first is not None
        assert first["kind"] == "learning_update"

        time_core.set_clock(lambda: started_at + timedelta(minutes=9, seconds=59))
        assert maybe_propose_from_turn(config, turn={}) is None

        rows = [
            row for row in jsonl.read_all(config.paths.journal("self_improvement"))
            if row.get("kind") == "self_improvement.auto_proposal"
        ]
        assert len(rows) == 1
        assert rows[0]["trigger"] == "consecutive_noops"
    finally:
        time_core.reset_clock()


def test_noop_auto_proposal_allows_after_cooldown(tmp_path):
    config = _config(tmp_path)
    started_at = datetime(2026, 5, 3, 1, 0, tzinfo=timezone.utc)
    time_core.set_clock(lambda: started_at)
    try:
        _append_noop_turns(config)

        assert maybe_propose_from_turn(config, turn={}) is not None

        time_core.set_clock(lambda: started_at + timedelta(minutes=10, seconds=1))
        assert maybe_propose_from_turn(config, turn={}) is not None

        rows = [
            row for row in jsonl.read_all(config.paths.journal("self_improvement"))
            if row.get("kind") == "self_improvement.auto_proposal"
        ]
        assert len(rows) == 2
    finally:
        time_core.reset_clock()


def test_noop_auto_proposal_cooldown_can_be_disabled(tmp_path):
    config = _config(
        tmp_path,
        {
            "agent": {
                "native": {
                    "self_improvement_noop_cooldown_seconds": 0,
                },
            },
        },
    )
    _append_noop_turns(config)

    assert maybe_propose_from_turn(config, turn={}) is not None
    assert maybe_propose_from_turn(config, turn={}) is not None
