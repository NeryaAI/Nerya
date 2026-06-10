"""List recurring schedules and background task records."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from nerya.core.config import load_config
from nerya.subagents.tasks import TaskStore
from nerya.triggers.schedule import load_schedules


def run(
    payload: dict[str, Any] | None = None,
    *,
    workspace: str | Path | None = None,
) -> dict[str, Any]:
    payload = dict(payload or {})
    cfg = load_config(Path(workspace).expanduser() if workspace else None)
    limit = max(1, int(payload.get("limit") or 25))
    session_id = str(payload.get("session_id") or "").strip() or None
    state = str(payload.get("state") or "").strip() or None

    schedules = [
        {
            "id": e.id,
            "kind": e.kind,
            "enabled": e.enabled,
            "cron": e.cron,
            "every_seconds": e.every_seconds,
            "timezone": e.timezone,
            "session_kind": e.session_kind,
            "session_mode": e.session_mode,
            "delivery_targets": [dict(t) for t in e.delivery_targets or []],
        }
        for e in load_schedules(cfg.paths)
        if e.session_kind in {"agent", "script"}
    ][:limit]

    store = TaskStore(cfg.paths)
    records = store.list(
        state=state,
        parent_session_id=session_id,
        limit=limit,
    )
    background_tasks = [
        {
            "task_id": r.task_id,
            "name": r.name,
            "state": r.state,
            "started_at": r.started_at,
            "finished_at": r.finished_at,
            "progress_count": len(r.progress),
            "parent_session_id": r.parent_session_id,
            "strategy_id": r.strategy_id,
        }
        for r in records
    ]

    return {
        "ok": True,
        "schedules": schedules,
        "background_tasks": background_tasks,
        "counts": {
            "schedules": len(schedules),
            "background_tasks": len(background_tasks),
        },
    }


def _load_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.payload_file:
        with open(args.payload_file, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    if args.payload_json:
        return json.loads(args.payload_json) or {}
    if not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
        return json.loads(raw) if raw else {}
    return {}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", dest="payload_json", default=None)
    parser.add_argument("--payload-file", dest="payload_file", default=None)
    parser.add_argument("--workspace", dest="workspace", default=None)
    args = parser.parse_args()

    try:
        result = run(_load_payload(args), workspace=args.workspace)
    except Exception as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        raise SystemExit(1)
    sys.stdout.write(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
