from datetime import datetime, timedelta, timezone
from copy import deepcopy
import pytest
from nerya.core.paths import WorkspacePaths
from nerya.strategy_history.store import record_agent_task
from nerya.strategies.agent_review_evidence import agent_review_evidence

pytestmark = pytest.mark.smoke
ANCHOR = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)


def row(**changes):
    return {"task_id": "t1", "strategy_id": "alpha", "package_hash": "hash-a", "session_id": "s1",
            "status": "executed", "ts": (ANCHOR-timedelta(minutes=1)).isoformat(),
            "metadata": {"path": "opportunity", "input_context": {"path": "agent_tasks/t1/context.json"}},
            "final_text": "selected output", "tool_trace": [{"private": "do not include"}], **changes}


def append(paths, task, session="s1"):
    record_agent_task(paths, strategy_id="alpha", session_id=session, task=task)


def read(paths, **options):
    return agent_review_evidence(paths, strategy_id="alpha", package_hash="hash-a", package_mode="paper", execution_mode="paper",
        cutoff=ANCHOR-timedelta(days=1), anchor=ANCHOR, limit=10, **options)


def test_terminal_is_one_record_and_excludes_private_traces(tmp_path):
    paths = WorkspacePaths(tmp_path)
    append(paths, row(status="running", ts=(ANCHOR-timedelta(minutes=2)).isoformat()))
    append(paths, row())
    out = read(paths)
    assert len(out["tasks"]) == 1 and out["tasks"][0]["status"] == "executed"
    assert out["tasks"][0]["path"] == "opportunity"
    assert "tool_trace" not in out["tasks"][0]
    assert "do not include" not in str(out)


@pytest.mark.parametrize("changes,reason", [({"strategy_id":"other"},"strategy_id"),({"package_hash":"old"},"package_hash"),({"package_hash":None},"package_hash"),({"mode":"live"},"execution_mode"),({"ts":(ANCHOR+timedelta(days=1)).isoformat()},"timestamp"),({"ts":(ANCHOR-timedelta(days=2)).isoformat()},"timestamp")])
def test_other_version_strategy_mode_or_time_is_excluded(tmp_path, changes, reason):
    paths = WorkspacePaths(tmp_path); append(paths, row(**changes))
    out = read(paths)
    assert out["tasks"] == [] and out["excluded"][reason] == 1


def test_request_selection_and_conflicting_session(tmp_path):
    paths = WorkspacePaths(tmp_path)
    append(paths, row())
    assert read(paths, requested_run_ids={"other"})["tasks"] == []
    assert read(paths, requested_session_ids={"other"})["tasks"] == []
    assert len(read(paths, requested_session_ids={"s1"})["tasks"]) == 1
    append(paths, row(task_id="t2"), session="different")
    assert len(read(paths)["tasks"]) == 1


def test_skip_without_session_is_valid_and_not_called_success(tmp_path):
    paths = WorkspacePaths(tmp_path)
    append(paths, row(status="skipped", session_id=None, metadata={"path":"stop"}), session=None)
    out=read(paths)
    assert out["by_status"] == {"skipped": 1}
    assert out["tasks"][0]["path"] == "stop"


def test_missing_hash_terminal_does_not_resurrect_running(tmp_path):
    paths = WorkspacePaths(tmp_path)
    append(paths, row(status="running", ts=(ANCHOR-timedelta(minutes=2)).isoformat()))
    append(paths, row(package_hash=None, status="failed"))
    assert read(paths)["tasks"] == []


def test_long_output_is_explicit_preview(tmp_path):
    paths=WorkspacePaths(tmp_path); append(paths,row(final_text="public evidence. "*400))
    task=read(paths)["tasks"][0]
    assert len(task["final_text"]) == 4000 and task["final_text_truncated"] is True


def test_secret_shaped_output_remains_redacted(tmp_path):
    paths=WorkspacePaths(tmp_path); append(paths,row(final_text="x"*5000))
    task=read(paths)["tasks"][0]
    assert task["final_text"] == "***REDACTED***"
    assert task["final_text_truncated"] is True
