"""HTTP routes for the agent-team subsystem.

These endpoints surface the durable team state under
``workspace/teams/`` so the dashboard / CLI can list runs and inspect
the synthesis output without going through the agent.
"""

from __future__ import annotations

from typing import Any

from ..subagents.registry import (
    delete_role,
    describe_role,
    list_roles,
    save_role,
)
from ..teams.store import TeamStore
from ..teams.templates import BUILTIN_TEMPLATES, list_templates
from ..teams.orchestrator import TeamOrchestrator


def _list_templates(client, _payload):  # noqa: ARG001
    return {
        "ok": True,
        "templates": list_templates(),
        "ids": list(BUILTIN_TEMPLATES.keys()),
    }


def _list_runs(client, payload):
    payload = payload or {}
    store = TeamStore(client.config.paths)
    limit = int(payload.get("limit") or 50)
    return {"ok": True, "runs": store.list_runs(limit=limit)}


def _get_run(client, payload):
    payload = payload or {}
    run_id = str(payload.get("run_id") or "").strip()
    if not run_id:
        return {"ok": False, "error": "run_id required"}
    store = TeamStore(client.config.paths)
    run = store.read_run(run_id)
    if run is None:
        return {"ok": False, "error": f"run {run_id!r} not found"}
    syn_dir = store.synthesis_dir(run_id)
    final_report = ""
    final_context: dict[str, Any] | None = None
    fr = syn_dir / "final_report.md"
    if fr.exists():
        try:
            final_report = fr.read_text(encoding="utf-8")
        except OSError:
            final_report = ""
    fc = syn_dir / "final_context.json"
    if fc.exists():
        try:
            import json as _json
            final_context = _json.loads(fc.read_text(encoding="utf-8"))
        except Exception:
            final_context = None
    return {
        "ok": True,
        "run": run.asdict(),
        "members": [m.asdict() for m in store.read_members(run_id)],
        "tasks": [t.asdict() for t in store.list_tasks(run_id)],
        "events": store.list_events(run_id),
        "messages": __read_messages(store, run_id),
        "blackboard": __read_blackboard(store, run_id),
        "artifacts": __read_artifacts(store, run_id),
        "final_report": final_report,
        "final_context": final_context,
    }


def _start_run(client, payload):
    payload = payload or {}
    template = str(payload.get("template") or "").strip()
    goal = str(payload.get("goal") or "").strip()
    if not template:
        return {"ok": False, "error": "template required"}
    if not goal:
        return {"ok": False, "error": "goal required"}
    if template not in BUILTIN_TEMPLATES:
        return {
            "ok": False,
            "error": f"unknown template {template!r}",
            "available": sorted(BUILTIN_TEMPLATES.keys()),
        }
    orch = TeamOrchestrator(config=client.config, skills=client.skills)
    res = orch.run(
        template=template,
        goal=goal,
        trigger=payload.get("trigger") or {"kind": "http", "payload": {}},
        memory_preview=payload.get("memory_preview"),
        strategy_id=payload.get("strategy_id"),
        session_id=payload.get("session_id"),
    )
    body = res.asdict()
    body["ok"] = res.status == "completed"
    return body


def __read_blackboard(store: TeamStore, run_id: str):
    from ..core import jsonl
    return jsonl.read_all(store.blackboard_path(run_id))


def __read_messages(store: TeamStore, run_id: str) -> list[dict[str, Any]]:
    import json as _json

    root = store.run_dir(run_id) / "inboxes"
    if not root.exists():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(root.rglob("msg-*.json")) + sorted(root.rglob("*.consumed")):
        try:
            row = _json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(row, dict):
            row.setdefault("mailbox_path", str(path))
            out.append(row)
    out.sort(key=lambda x: str(x.get("created_at") or ""))
    return out


def __read_artifacts(store: TeamStore, run_id: str) -> list[dict[str, Any]]:
    import json as _json

    root = store.artifacts_dir(run_id)
    if not root.exists():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        try:
            row = _json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(row, dict):
            row.setdefault("artifact_path", str(path))
            out.append(row)
    return out


# ---------------------------------------------------------------------------
# Persistent role CRUD
# ---------------------------------------------------------------------------


def _list_roles(client, _payload):  # noqa: ARG001
    return {"ok": True, "roles": list_roles(client.config.paths)}


def _get_role(client, payload):
    payload = payload or {}
    name = str(payload.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "name required"}
    record = describe_role(client.config.paths, name)
    if record is None:
        return {"ok": False, "error": f"role {name!r} not found"}
    return {"ok": True, "role": record}


def _save_role(client, payload):
    payload = payload or {}
    try:
        record = save_role(
            client.config.paths,
            name=str(payload.get("name") or "").strip(),
            prompt=str(payload.get("prompt") or ""),
            allowed_skills=list(payload.get("allowed_skills") or []) or None,
            tier=payload.get("tier"),
        )
    except (ValueError, OSError) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "role": record}


def _delete_role(client, payload):
    payload = payload or {}
    name = str(payload.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "name required"}
    try:
        deleted = delete_role(client.config.paths, name)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "deleted": bool(deleted), "name": name}


def routes():
    return [
        ("GET", "/teams/templates", _list_templates),
        ("POST", "/teams/templates", _list_templates),
        ("GET", "/teams/runs", _list_runs),
        ("POST", "/teams/runs", _list_runs),
        ("POST", "/teams/run", _start_run),
        ("POST", "/teams/get", _get_run),
        # +: persistent agent role registry (workspace +
        # defaults). The dashboard renders this under
        # /agent/roles; the model can also call role_* tools to
        # introspect or update roles between turns.
        ("GET", "/teams/roles", _list_roles),
        ("POST", "/teams/roles", _list_roles),
        ("POST", "/teams/role/get", _get_role),
        ("POST", "/teams/role/save", _save_role),
        ("POST", "/teams/role/delete", _delete_role),
    ]
