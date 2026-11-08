from __future__ import annotations

import copy

import pytest

from nerya.agent.task_control import (
    create_mandate,
    dispatch_trigger,
    public_control,
    subscribe_task,
    trigger_health,
    list_dead_letters,
    retry_due_trigger_deliveries,
    replay_dead_letter,
)
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.triggers.event import TriggerEvent


pytestmark = pytest.mark.smoke


def _task(tmp_path):
    paths = WorkspacePaths(tmp_path)
    create_mandate(paths, task_id="awork_trigger", command_id="cmd_initial", session_id="task_session",
                   actor_id="owner", task_kind="research", objective="Monitor BTC", scope={"paper_only": True},
                   allowed_tools=["research"])
    return paths


def test_trigger_event_delivery_is_filtered_and_idempotent(tmp_path, monkeypatch):
    paths = _task(tmp_path)
    subscribe_task(paths, task_id="awork_trigger", source="schedule", kind="market.review", match={"payload.market": "BTC"})
    subscribe_task(paths, task_id="awork_trigger", source="schedule", kind="market.review", match={"payload.market": "ETH"})
    config = Config(paths=paths, data=copy.deepcopy(DEFAULT_CONFIG))
    delivered = []

    def enqueue(_self, _config, task_id, event):
        delivered.append((task_id, event["event_id"]))
        return {"state": "delivered", "input_id": "input-1", "command_id": "cmd-trigger"}

    from nerya.agent.assistant_work import AssistantWork
    monkeypatch.setattr(AssistantWork, "enqueue_trigger", enqueue)
    event = {"event_id": "evt-schedule-1", "source": "schedule", "kind": "market.review",
             "source_cursor": "schedule:review:100", "idempotency_key": "schedule:review:100",
             "occurred_at": "2026-11-05T00:00:00Z", "payload": {"market": "BTC"}}
    first = dispatch_trigger(paths, event, config=config)
    second = dispatch_trigger(paths, event, config=config)
    assert first["matched"] == 1 and first["state"] == "delivered"
    assert second["deliveries"][0]["state"] == "deduped"
    assert delivered == [("awork_trigger", "evt-schedule-1")]
    control = public_control(paths, "awork_trigger")
    assert control["trigger_deliveries"][0]["state"] == "delivered"


def test_trigger_delivery_failure_is_visible_in_health(tmp_path, monkeypatch):
    paths = _task(tmp_path)
    subscribe_task(paths, task_id="awork_trigger", source="schedule", kind="", match={})
    config = Config(paths=paths, data=copy.deepcopy(DEFAULT_CONFIG))
    from nerya.agent.assistant_work import AssistantWork
    monkeypatch.setattr(AssistantWork, "enqueue_trigger", lambda *_args: (_ for _ in ()).throw(RuntimeError("queue unavailable")))
    result = dispatch_trigger(paths, {"event_id": "evt-fail", "source": "schedule", "kind": "review",
                                     "source_cursor": "cursor-1", "payload": {}}, config=config)
    assert result["state"] == "retry_scheduled" and result["deliveries"][0]["state"] == "retry_scheduled"
    health = trigger_health(paths, "schedule")
    assert health[0]["state"] == "degraded" and "queue unavailable" in health[0]["last_error"]


def test_failed_delivery_retries_to_dead_letter_then_replays(tmp_path, monkeypatch):
    paths = _task(tmp_path)
    subscribe_task(paths, task_id="awork_trigger", source="schedule", kind="review", match={})
    config = Config(paths=paths, data=copy.deepcopy(DEFAULT_CONFIG))
    from nerya.agent.assistant_work import AssistantWork
    monkeypatch.setattr(AssistantWork, "enqueue_trigger", lambda *_args: (_ for _ in ()).throw(RuntimeError("down")))
    first = dispatch_trigger(paths, {"event_id": "evt-retry", "source": "schedule", "kind": "review", "source_cursor": "c1", "payload": {}}, config=config)
    assert first["state"] == "retry_scheduled"
    from nerya.db.sqlite import connect
    with connect(paths.db) as con:
        con.execute("UPDATE task_trigger_deliveries SET next_retry_at=0 WHERE event_id='evt-retry'")
        con.commit()
    retry_due_trigger_deliveries(paths, config=config, now=1000)
    with connect(paths.db) as con:
        con.execute("UPDATE task_trigger_deliveries SET next_retry_at=0 WHERE event_id='evt-retry'")
        con.commit()
    second = retry_due_trigger_deliveries(paths, config=config, now=2000)
    assert second["results"][0]["state"] == "dead_letter"
    dead = list_dead_letters(paths)
    assert len(dead) == 1 and dead[0]["attempts"] == 3
    monkeypatch.setattr(AssistantWork, "enqueue_trigger", lambda _self, _config, _task, _event: {"state": "delivered", "input_id": "i", "command_id": "c"})
    replay = replay_dead_letter(paths, dead_letter_id=dead[0]["dead_letter_id"], config=config)
    assert replay["replay"]["state"] == "delivered"
    assert list_dead_letters(paths) == []


def test_trigger_event_has_stable_schedule_identity():
    first = TriggerEvent(event_id="evt_schedule_review_100", source="schedule", kind="review", payload={})
    second = TriggerEvent(event_id="evt_schedule_review_100", source="schedule", kind="review", payload={})
    assert first.event_id == second.event_id
