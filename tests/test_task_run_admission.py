from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

from nerya.agent.command_runtime import CommandRuntime
from nerya.agent.command_store import CommandError
from nerya.agent.task_runs import TaskRuns, TaskSpec
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.api.route_scopes import required_scope, authorize

pytestmark = pytest.mark.smoke


def runs(tmp_path):
    manager=CommandRuntime(Config(WorkspacePaths(tmp_path),deepcopy(DEFAULT_CONFIG)),lambda *_:{},epoch="test")
    manager.accepted_model=lambda _: {"provider":"fake","model":"fake-model"}
    return TaskRuns(manager)


def spec(policy="skip"):
    return TaskSpec("scheduled_agent","daily","session-daily","v1","Daily review",
                    {"payload":{"text":"Inspect today's data"}},overlap_policy=policy)


def admit(store,key="occurrence-1",policy="skip",**kw):
    return store.admit(spec(policy),actor="operator",trigger={"value":kw.pop("value",1)},
                       trigger_id=key,start=False,**kw)


def test_atomic_duplicate_and_conflict(tmp_path):
    store=runs(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts=list(pool.map(lambda _:admit(store),range(12)))
    assert sum(not r["duplicate"] for r in receipts)==1
    assert len(store.store.snapshot("session-daily")["commands"])==1
    with pytest.raises(CommandError,match="run_idempotency_conflict"):
        admit(store,value=2)


def test_failed_command_admission_rolls_back_run(tmp_path,monkeypatch):
    store=runs(tmp_path)
    def fail(**kw):raise CommandError("queue_full",429)
    monkeypatch.setattr(store.store,"accept",fail)
    with pytest.raises(CommandError,match="queue_full"):admit(store)
    assert store.list()["runs"]==[]


def test_skip_and_latest_coalescing(tmp_path):
    store=runs(tmp_path)
    first=admit(store)
    assert admit(store,"second")["admission_status"]=="skipped"
    next_run=admit(store,"third","coalesce")
    assert next_run["admission_status"]=="admitted"
    assert store.get(first["run_id"])["reason"]=="coalesced"
    assert len([c for c in store.store.snapshot("session-daily")["commands"] if c["state"]=="queued"])==1


def test_owner_and_stable_cursor(tmp_path):
    store=runs(tmp_path)
    a=admit(store)
    for i in range(10):admit(store,str(i))
    page=store.list(limit=3)
    admit(store,"new")
    next_page=store.list(limit=3,cursor=page["next_cursor"])
    assert not ({r["run_id"] for r in page["runs"]}&{r["run_id"] for r in next_page["runs"]})
    with pytest.raises(CommandError,match="run_owner_mismatch"):store.get(a["run_id"],actor="other")
    assert store.list(actor="other")["runs"]==[]


def test_run_and_command_states_are_distinct(tmp_path):
    store=runs(tmp_path);receipt=admit(store)
    claimed=store.store.claim("session-daily","worker")
    store.store.finish(claimed["command_id"],"session-daily","worker","awaiting_approval",{"final_text":"Order awaits approval"})
    projection=store.get(receipt["run_id"])
    assert projection["execution_status"]=="awaiting_approval"
    assert projection["business_status"]=="reported"
    assert admit(store,"another")["admission_status"]=="skipped"


def test_read_scope_cannot_tick():
    required=required_scope("POST","/triggers/schedules/tick")
    assert required=="execute:automation"
    assert not authorize({"read:runtime"},"POST","/triggers/schedules/tick")[0]


def test_busy_manual_receipt_survives_original_completion(tmp_path):
    store=runs(tmp_path)
    first=admit(store,"one",trigger_kind="manual",client_request_id="one")
    busy=admit(store,"two",trigger_kind="manual",client_request_id="two")
    assert busy["duplicate"] and busy["run_id"]==first["run_id"]
    command=store.store.claim("session-daily","owner")
    store.store.finish(command["command_id"],"session-daily","owner","succeeded",{"stopped_reason":"completed"})
    again=admit(store,"two",trigger_kind="manual",client_request_id="two")
    assert again["duplicate"] and again["run_id"]==first["run_id"]
    assert store.list(actor="operator",client_request_id="two")["runs"][0]["run_id"]==first["run_id"]


def test_calendar_fold_selects_earliest_future_utc():
    from datetime import datetime
    from nerya.triggers.schedule import ScheduleEntry
    from nerya.triggers.schedule_clock import next_due
    entry=ScheduleEntry(id="dst",kind="agent",cron="* * * * *",timezone="America/New_York")
    after=datetime.fromisoformat("2026-11-01T05:15:00+00:00").timestamp()
    assert next_due(entry,after)==after+60
