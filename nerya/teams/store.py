"""Durable, file-backed store for :mod:`nerya.teams` runs.

Layout under ``<workspace>/teams/<run_id>/``::

    run.json
    template.json
    members.json
    tasks/<task_id>.json
    inboxes/<agent>/msg-<id>.json
    events.jsonl
    blackboard.jsonl
    artifacts/<artifact_id>.json
    synthesis/{conflict_matrix,final_context,final_report}.{json,md}

Writes are atomic via temp+rename so a crashed run leaves recoverable
state. The store has zero external dependencies and never calls an LLM.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from ..core import jsonl
from ..core.paths import WorkspacePaths
from ..core.time import now_iso
from .models import (
    BlackboardEntry,
    TeamMember,
    TeamMessage,
    TeamRun,
    TeamTask,
    TeamTemplate,
)


def _slug(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in value)[:64]


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp-{uuid.uuid4().hex[:8]}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


class TeamStore:
    """File-backed store for team runs.

    The constructor only takes :class:`WorkspacePaths`; everything else
    is derived. Each instance is stateless beyond its root path so it is
    safe to construct per-call.
    """

    def __init__(self, paths: WorkspacePaths):
        self.paths = paths
        self.root = paths.root / "teams"

    # ------------------------------------------------------------ paths
    def run_dir(self, run_id: str) -> Path:
        return self.root / _slug(run_id)

    def runs_root(self) -> Path:
        return self.root

    def task_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "tasks"

    def task_path(self, run_id: str, task_id: str) -> Path:
        return self.task_dir(run_id) / f"{_slug(task_id)}.json"

    def inbox_dir(self, run_id: str, agent: str) -> Path:
        return self.run_dir(run_id) / "inboxes" / _slug(agent)

    def blackboard_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "blackboard.jsonl"

    def events_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "events.jsonl"

    def artifacts_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "artifacts"

    def synthesis_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "synthesis"

    # ------------------------------------------------------------ creates
    def create_run(self, run: TeamRun, template: TeamTemplate, members: list[TeamMember]) -> None:
        d = self.run_dir(run.id)
        d.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(d / "run.json", run.asdict())
        _write_json_atomic(d / "template.json", template.asdict())
        _write_json_atomic(d / "members.json", [m.asdict() for m in members])
        self.append_event(run.id, kind="run.created", template=template.id, goal=run.goal)

    def create_task(self, task: TeamTask) -> None:
        path = self.task_path(task.run_id, task.id)
        _write_json_atomic(path, task.asdict())
        self.append_event(
            task.run_id,
            kind="task.created",
            task_id=task.id,
            owner=task.owner,
            subagent=task.subagent_name,
            subject=task.subject,
            description=task.description,
            depends_on=list(task.depends_on),
            required=task.required,
        )

    # ------------------------------------------------------------ reads
    def read_run(self, run_id: str) -> Optional[TeamRun]:
        data = _read_json(self.run_dir(run_id) / "run.json")
        if not data:
            return None
        return _build(TeamRun, data)

    def read_members(self, run_id: str) -> list[TeamMember]:
        data = _read_json(self.run_dir(run_id) / "members.json") or []
        if not isinstance(data, list):
            return []
        return [_build(TeamMember, d) for d in data]

    def read_template(self, run_id: str) -> Optional[dict[str, Any]]:
        return _read_json(self.run_dir(run_id) / "template.json")

    def list_tasks(self, run_id: str) -> list[TeamTask]:
        d = self.task_dir(run_id)
        if not d.exists():
            return []
        out: list[TeamTask] = []
        for path in sorted(d.glob("*.json")):
            data = _read_json(path)
            if data:
                out.append(_build(TeamTask, data))
        return out

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        rows: list[dict[str, Any]] = []
        for child in sorted(self.root.iterdir(), reverse=True):
            if not child.is_dir():
                continue
            data = _read_json(child / "run.json")
            if data:
                if "template" not in data and data.get("template_id"):
                    data["template"] = data["template_id"]
                rows.append(data)
            if len(rows) >= limit:
                break
        return rows

    # ------------------------------------------------------------ updates
    def update_run(self, run: TeamRun) -> None:
        run.updated_at = now_iso()
        _write_json_atomic(self.run_dir(run.id) / "run.json", run.asdict())
        self.append_event(
            run.id,
            kind="run.updated",
            status=run.status,
            phase=run.phase,
            error=run.error,
            metrics=run.metrics,
        )

    def update_task(self, task: TeamTask) -> None:
        _write_json_atomic(self.task_path(task.run_id, task.id), task.asdict())
        self.append_event(
            task.run_id,
            kind="task.updated",
            task_id=task.id,
            owner=task.owner,
            subagent=task.subagent_name,
            subject=task.subject,
            status=task.status,
            error=task.error,
            artifact=task.result_artifact,
            summary=task.result_summary,
            payload=task.payload,
            input_payload=(task.payload or {}).get("input_payload"),
            assignment_prompt=(task.payload or {}).get("assignment_prompt"),
            started_at=task.started_at,
            completed_at=task.completed_at,
        )

    # ------------------------------------------------------------ events
    def append_event(self, run_id: str, *, kind: str, **fields: Any) -> dict[str, Any]:
        rec = {"kind": kind, "ts": now_iso(), **fields}
        written = jsonl.append(self.events_path(run_id), rec, stamp=False)
        try:
            from ..agent.streaming import get_default_bus

            get_default_bus().publish(
                "team.event",
                team_run_id=run_id,
                team_event_kind=kind,
                team_event=written,
                **fields,
            )
        except Exception:
            pass
        return written

    def list_events(self, run_id: str) -> list[dict[str, Any]]:
        return jsonl.read_all(self.events_path(run_id))

    # ------------------------------------------------------------ artifacts
    def write_artifact(self, run_id: str, payload: dict[str, Any], *, kind: str = "result") -> str:
        artifact_id = f"{kind}-{uuid.uuid4().hex[:10]}"
        d = self.artifacts_dir(run_id)
        d.mkdir(parents=True, exist_ok=True)
        body = {
            "artifact_id": artifact_id,
            "kind": kind,
            "created_at": now_iso(),
            "payload": payload,
        }
        _write_json_atomic(d / f"{artifact_id}.json", body)
        self.append_event(
            run_id,
            kind="artifact.written",
            artifact_id=artifact_id,
            artifact_kind=kind,
            task_id=payload.get("task_id"),
            owner=payload.get("owner"),
            subagent=payload.get("subagent"),
            summary=payload.get("summary"),
            signal=payload.get("signal"),
            confidence=payload.get("confidence"),
        )
        return artifact_id

    def read_artifact(self, run_id: str, artifact_id: str) -> Optional[dict[str, Any]]:
        return _read_json(self.artifacts_dir(run_id) / f"{artifact_id}.json")

    # ------------------------------------------------------------ synthesis
    def write_synthesis_json(self, run_id: str, name: str, payload: dict[str, Any]) -> Path:
        d = self.synthesis_dir(run_id)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{_slug(name)}.json"
        _write_json_atomic(path, payload)
        self.append_event(
            run_id,
            kind="synthesis.written",
            name=name,
            path=str(path),
            format="json",
        )
        return path

    def write_synthesis_text(self, run_id: str, name: str, text: str) -> Path:
        d = self.synthesis_dir(run_id)
        d.mkdir(parents=True, exist_ok=True)
        # Preserve the file extension; only sanitise the stem so callers
        # can pass things like ``final_report.md`` and get the ``.md``
        # back on disk.
        if "." in name:
            stem, _, ext = name.rpartition(".")
            safe = f"{_slug(stem)}.{_slug(ext)}"
        else:
            safe = _slug(name)
        path = d / safe
        path.write_text(text, encoding="utf-8")
        self.append_event(
            run_id,
            kind="synthesis.written",
            name=name,
            path=str(path),
            format="text",
            bytes=len(text.encode("utf-8")),
        )
        return path

    # ------------------------------------------------------------ delete
    def delete_run(self, run_id: str) -> None:
        d = self.run_dir(run_id)
        if d.exists():
            shutil.rmtree(d)


def _build(cls, data: dict[str, Any]):
    """Build a dataclass from a dict, ignoring unknown keys."""

    fields = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
    kwargs = {k: v for k, v in data.items() if k in fields}
    return cls(**kwargs)


__all__ = ["TeamStore"]
