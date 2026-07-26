from __future__ import annotations

from nerya.core import jsonl
from nerya.progress.todo import format_for_injection as progress_format_for_injection
from nerya.rollout.writer import RolloutWriter, Turn
from nerya.security.audit import record as record_security_audit
from nerya.skills.registry import list_bundled_skill_names
from nerya.tools.native.task import TaskState, TodoItem, format_for_injection


def test_rollout_writer_appends_redacted_turn_jsonl(tmp_path) -> None:
    path = tmp_path / "rollout.jsonl"
    written = RolloutWriter(path).write(
        Turn(
            turn_id="turn_1",
            session_id="sess_1",
            payload={"api_key": "sk-secret-value-12345678901234567890"},
        )
    )

    assert written["turn_id"] == "turn_1"
    rows = jsonl.read_all(path)
    assert rows[0]["payload"]["api_key"]["__redacted__"] is True


def test_security_audit_records_audit_event_jsonl(tmp_path) -> None:
    path = tmp_path / "security.jsonl"

    record_security_audit(
        path,
        kind="sandbox_denied",
        caller="run_shell",
        payload={"authorization": "Bearer secret-token-12345678901234567890"},
    )

    row = jsonl.read_all(path)[0]
    assert row["audit_event"]["kind"] == "sandbox_denied"
    assert row["audit_event"]["payload"]["authorization"]["__redacted__"] is True


def test_jsonl_write_all_replaces_existing_rows(tmp_path) -> None:
    path = tmp_path / "records.jsonl"
    jsonl.append(path, {"id": "old"}, stamp=False)

    written = jsonl.write_all(path, [{"id": "new"}])

    assert written == [{"id": "new"}]
    assert jsonl.read_all(path) == written


def test_task_progress_formats_unfinished_work_for_injection() -> None:
    state = TaskState(
        todos=[
            TodoItem(id="t1", content="Collect logs", status="completed"),
            TodoItem(id="t2", content="Run R5 lint", activeForm="Running R5 lint", status="in_progress"),
            TodoItem(id="t3", content="Run full CSV", activeForm="Running full CSV", status="pending"),
        ]
    )

    text = format_for_injection(state)

    assert text.startswith("# Task Progress")
    assert "in_progress: Running R5 lint" in text
    assert "pending: Running full CSV" in text
    assert "Collect logs" not in text
    assert progress_format_for_injection(state) == text


def test_bundled_skill_allowlist_lists_builtin_skills() -> None:
    names = list_bundled_skill_names()

    assert "research" in names
    assert "strategy_author" in names
