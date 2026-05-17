from __future__ import annotations

from datetime import datetime, timezone

import pytest

from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.evolution.periodic_reflection import (
    PERIODIC_REFLECTION_SCHEDULE_ID,
    PERIODIC_REFLECTION_TARGET,
    configure_periodic_reflection,
    get_periodic_reflection,
)
from nerya.triggers.event import TriggerEvent
from nerya.triggers.runtime import TriggerRuntime
from nerya.triggers.schedule import ScheduleEntry, load_schedules

pytestmark = pytest.mark.smoke


def test_periodic_reflection_config_roundtrips_schedule(tmp_path):
    paths = WorkspacePaths(tmp_path)

    default = get_periodic_reflection(paths)
    assert default["enabled"] is False
    assert default["configured"] is False
    assert default["time"] == "03:00"

    out = configure_periodic_reflection(
        paths,
        enabled=True,
        time="02:30",
        timezone="Asia/Shanghai",
    )

    assert out["ok"] is True
    assert out["schedule"]["enabled"] is True
    assert out["schedule"]["cron"] == "30 2 * * *"
    entry = next(e for e in load_schedules(paths) if e.id == PERIODIC_REFLECTION_SCHEDULE_ID)
    assert entry.target == PERIODIC_REFLECTION_TARGET
    assert entry.timezone == "Asia/Shanghai"


def test_cron_schedule_uses_entry_timezone():
    entry = ScheduleEntry(
        id="dream",
        kind="evolution.reflect",
        cron="0 3 * * *",
        target=PERIODIC_REFLECTION_TARGET,
        timezone="Asia/Shanghai",
    )

    assert entry.is_due(
        now=datetime(2026, 1, 1, 19, 0, tzinfo=timezone.utc),
        last_fired=None,
    )
    assert not entry.is_due(
        now=datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc),
        last_fired=None,
    )


def test_evolution_reflection_trigger_target_executes_runner(tmp_path, monkeypatch):
    config = Config(paths=WorkspacePaths(tmp_path), data={})
    runtime = TriggerRuntime.boot(config)

    def fake_evolve(_config):
        return {
            "proposal": {"id": "prop_reflect_1"},
            "ranked": [{"id": "seed"}],
            "signals": [{"id": "sig_1"}],
            "event": {"id": "evo_1"},
        }

    monkeypatch.setattr("nerya.evolution.runner.evolve", fake_evolve)
    event = TriggerEvent.new(
        source="schedule",
        kind="evolution.reflect",
        payload={"reason": "test"},
        target=PERIODIC_REFLECTION_TARGET,
    )

    result = runtime.emit(event)

    assert result.status == "executed"
    assert result.target == PERIODIC_REFLECTION_TARGET
    assert result.strategy_id is None
    assert result.result["proposal_id"] == "prop_reflect_1"
    assert result.result["ranked_count"] == 1
    assert result.result["signal_count"] == 1
