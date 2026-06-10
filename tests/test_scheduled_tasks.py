from __future__ import annotations

from types import SimpleNamespace

import pytest

from nerya.core import yaml_io
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.messaging.scheduled_delivery import deliver_scheduled_session
from nerya.triggers import cron as cron_mod
from nerya.triggers.cron import CronScheduler
from nerya.triggers.schedule import ScheduleEntry, load_schedules, save_schedules
from nerya.triggers.scheduled_session import ScheduledSessionRunner


pytestmark = pytest.mark.smoke


class _NoRouteRuntime:
    def emit(self, _event):
        raise AssertionError("script schedule should not enter trigger routing")


def _config(tmp_path) -> Config:
    return Config(paths=WorkspacePaths(tmp_path), data={})


def _write_approved_script(paths: WorkspacePaths, script_id: str) -> None:
    script_dir = paths.scripts_approved / script_id
    script_dir.mkdir(parents=True, exist_ok=True)
    yaml_io.dump(
        script_dir / "manifest.yml",
        {
            "id": script_id,
            "version": "0.1.0",
            "title": "Digest",
            "description": "Digest",
            "entry": "run",
            "state": "approved",
        },
    )
    (script_dir / f"{script_id}.py").write_text(
        "def run(topic='chain'):\n"
        "    return {'summary': 'digest:' + topic}\n",
        encoding="utf-8",
    )


def test_schedule_entry_roundtrips_script_and_gateway_delivery(tmp_path):
    paths = WorkspacePaths(tmp_path)
    entry = ScheduleEntry(
        id="daily_chain_digest",
        kind="script.blockchain_digest",
        cron="0 11 * * *",
        timezone="Asia/Shanghai",
        target="script:blockchain_digest",
        session_kind="script",
        payload={"script_id": "blockchain_digest", "args": {"topic": "btc"}},
        delivery_targets=[{"kind": "gateway", "platform": "telegram"}],
    )

    save_schedules(paths, [entry])
    loaded = load_schedules(paths)[0]

    assert loaded.session_kind == "script"
    assert loaded.payload["script_id"] == "blockchain_digest"
    assert loaded.delivery_targets == [{"kind": "gateway", "platform": "telegram"}]


def test_cron_executes_approved_script_schedule_without_strategy(tmp_path):
    cfg = _config(tmp_path)
    _write_approved_script(cfg.paths, "blockchain_digest")
    save_schedules(
        cfg.paths,
        [
            ScheduleEntry(
                id="script_blockchain_digest",
                kind="script.blockchain_digest",
                every_seconds=1,
                target="script:blockchain_digest",
                session_kind="script",
                payload={
                    "script_id": "blockchain_digest",
                    "args": {"topic": "chain"},
                },
                delivery_targets=[{"kind": "gateway", "channel": "telegram"}],
            ),
        ],
    )

    scheduler = CronScheduler(
        cfg,
        _NoRouteRuntime(),
        delivery_fn=lambda _cfg, _entry, _result: [
            {"ok": True, "kind": "gateway", "channel": "telegram"}
        ],
    )
    fired = scheduler.tick(now_ts=1000.0)

    assert fired[0]["schedule_id"] == "script_blockchain_digest"
    assert fired[0]["script"]["ok"] is True
    assert fired[0]["script"]["script_id"] == "blockchain_digest"
    assert fired[0]["script"]["result"] == {"summary": "digest:chain"}
    assert fired[0]["script"]["delivery"][0]["channel"] == "telegram"


def test_cron_tick_skips_when_workspace_lock_is_held(tmp_path):
    cfg = _config(tmp_path)
    save_schedules(
        cfg.paths,
        [
            ScheduleEntry(
                id="locked_digest",
                kind="agent.digest",
                every_seconds=1,
                session_kind="agent",
                payload={"prompt": "summarise"},
            ),
        ],
    )

    scheduler = CronScheduler(cfg, _NoRouteRuntime())

    with cron_mod._cron_tick_lock(cfg.paths.state / "cron.lock") as acquired:
        assert acquired is True
        assert scheduler.tick(now_ts=1000.0) == []

    assert scheduler.tick(now_ts=1000.0)[0]["schedule_id"] == "locked_digest"


def test_scheduled_agent_can_reuse_one_session_or_fanout(tmp_path):
    cfg = _config(tmp_path)
    calls: list[dict] = []

    class FakeKernel:
        def run_turn(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                turn_id=f"turn_{len(calls)}",
                decision={"message": "ok"},
                actions=[],
                stopped_reason="done",
            )

    runner = ScheduledSessionRunner(
        config=cfg,
        kernel_factory=lambda _cfg: FakeKernel(),
        delivery_fn=None,
    )
    reuse = ScheduleEntry(
        id="daily_agent_digest",
        kind="agent.digest",
        every_seconds=1,
        session_kind="agent",
        session_mode="reuse",
        session_id="daily-digest-session",
        payload={"prompt": "summarise blockchain news"},
    )

    first = runner.run_many(reuse, now_ts=1000.0)
    second = runner.run_many(reuse, now_ts=1060.0)

    assert first[0].session_id == "daily-digest-session"
    assert second[0].session_id == "daily-digest-session"
    assert calls[-1]["session_id"] == "daily-digest-session"

    fanout = ScheduleEntry(
        id="multi_agent_digest",
        kind="agent.digest",
        every_seconds=1,
        session_kind="agent",
        session_mode="fanout",
        session_ids=["digest-a", "digest-b"],
        payload={"prompt": "summarise blockchain news"},
    )

    results = runner.run_many(fanout, now_ts=1120.0)

    assert [r.session_id for r in results] == ["digest-a", "digest-b"]
    assert [c["session_id"] for c in calls[-2:]] == ["digest-a", "digest-b"]


def test_gateway_delivery_target_maps_platform_to_message_channel(tmp_path):
    cfg = _config(tmp_path)
    calls: list[dict] = []

    class FakePipeline:
        def send(self, **kwargs):
            calls.append(kwargs)
            return {
                "delivered": True,
                "message_id": "msg_1",
                "rate_limited": False,
            }

    entry = SimpleNamespace(
        id="daily_chain_digest",
        target="main",
        strategy_id=None,
        session_kind="script",
        delivery_targets=[{"kind": "gateway", "platform": "telegram"}],
    )

    report = deliver_scheduled_session(
        cfg,
        entry,
        {
            "script_id": "blockchain_digest",
            "script_run_id": "run_1",
            "result": {"summary": "digest ready"},
        },
        pipeline=FakePipeline(),
    )

    assert report[0]["ok"] is True
    assert report[0]["kind"] == "gateway"
    assert report[0]["channel"] == "telegram"
    assert calls[0]["channel"] == "telegram"
    assert calls[0]["text"] == "digest ready"
