"""Workflow read/authoring service. No promotion, scheduler start or order IO."""
from __future__ import annotations

import copy
import json
import re
import threading
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..core import yaml_io
from ..core.paths import WorkspacePaths
from ..evolution.patch_proposal import create_proposal, list_proposals
from .workflow_graph import (
    MAX_FILE_BYTES, WORKFLOW_FILE, WorkflowError, build_workflows,
    package_revision, parse_metadata, safe_relative_path,
)

_LOCK = threading.RLock()
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SKIP_DIRS = {"runs", "logs", "state", "versions", "backtests", "history", "reviews", "sessions", "agent_tasks", "__pycache__", "secrets", "accounts"}
_MAX_TOTAL_BYTES = 4_000_000


def _safe_root(parent: Path, name: str) -> Path:
    if not isinstance(name, str) or not _ID.fullmatch(name):
        raise WorkflowError("Invalid strategy or proposal identifier")
    path = parent / name
    if path.is_symlink() or not path.resolve().is_relative_to(parent.resolve()):
        raise WorkflowError("Symlink or out-of-workspace package is not allowed")
    return path


def _read_files(root: Path) -> tuple[dict[str, str], list[str]]:
    if not root.is_dir():
        raise WorkflowError("Strategy package does not exist")
    files: dict[str, str] = {}
    omitted: list[str] = []
    total = 0
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(p.startswith(".") or p in _SKIP_DIRS for p in rel.parts):
            continue
        if path.is_symlink() or any(root.joinpath(*rel.parts[:i]).is_symlink() for i in range(1, len(rel.parts))):
            omitted.append(rel.as_posix())
            continue
        if not path.is_file() or path.suffix not in {".py", ".yml", ".yaml", ".md", ".json", ".txt"}:
            continue
        name = rel.as_posix()
        if name == "validation_report.json":
            continue
        size = path.stat().st_size
        if size > MAX_FILE_BYTES or total + size > _MAX_TOTAL_BYTES:
            omitted.append(name)
            continue
        try:
            with path.open("rb") as stream:
                blob = stream.read(MAX_FILE_BYTES + 1)
            if len(blob) > MAX_FILE_BYTES:
                omitted.append(name)
                continue
            files[name] = blob.decode("utf-8")
            total += len(blob)
        except UnicodeDecodeError:
            omitted.append(name)
    return files, omitted


def source_files(paths: WorkspacePaths, strategy_id: str, proposal_id: str | None = None,
                 *, _proposal_index: dict[str, Any] | None = None) -> tuple[dict[str, str], dict[str, Any]]:
    root = _safe_root(paths.strategies, strategy_id)
    if not proposal_id:
        files, omitted = _read_files(root)
        return files, {"proposal_id": None, "state": "published", "omitted_files": omitted}
    proposal_root = _safe_root(paths.proposals, proposal_id)
    # A directory request shares its own snapshot; never cache across requests.
    proposal = (_proposal_index.get(proposal_id) if _proposal_index is not None
                else next((p for p in list_proposals(paths) if p.id == proposal_id), None))
    if proposal is None or proposal.kind not in {"strategy_package_proposal", "strategy_tuning_proposal"}:
        raise WorkflowError("Strategy proposal not found")
    after = proposal_root / "after" / "strategies"
    if after.is_symlink() or (proposal_root / "after").is_symlink():
        raise WorkflowError("Symlink proposal payload is not allowed")
    staged = _safe_root(after, strategy_id)
    # Tuning proposals contain a delta. Overlay only on the same strategy.
    seed, omitted = _read_files(root) if root.is_dir() else ({}, [])
    changed, skipped = _read_files(staged)
    seed.update(changed)
    return seed, {"proposal_id": proposal_id, "state": proposal.state, "omitted_files": sorted(set(omitted + skipped))}


def view_workflow(paths: WorkspacePaths, strategy_id: str, proposal_id: str | None = None,
                  *, schedules: list[dict[str, Any]] | None = None,
                  _proposal_index: dict[str, Any] | None = None) -> dict[str, Any]:
    files, source = source_files(paths, strategy_id, proposal_id, _proposal_index=_proposal_index)
    out = build_workflows(files)
    declared_id = str(out["manifest"].get("strategy_id") or out["manifest"].get("id") or strategy_id)
    if declared_id != strategy_id:
        raise WorkflowError("Manifest strategy ID does not match the requested package")
    out.update(ok=True, strategy_id=strategy_id, source=source,
               can_edit=not bool(source["omitted_files"] or out["legacy"]),
               metadata=parse_metadata(files.get(WORKFLOW_FILE, "")))
    if schedules and not proposal_id:
        _attach_schedules(out, schedules, strategy_id)
    return out


def _attach_schedules(out: dict[str, Any], schedules: list[dict[str, Any]], strategy_id: str) -> None:
    """Installed jobs are distinct from a manifest's desired schedule."""
    graph = out["strategy"]
    index = 0
    for schedule in schedules:
        payload = schedule.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        if str(schedule.get("strategy_id") or payload.get("strategy_id") or "") != strategy_id:
            continue
        sid = str(schedule.get("id") or "")
        if not sid:
            continue
        nid = f"scheduler:installed/{sid}"
        graph["nodes"].append({
            "id": nid, "kind": "scheduler", "title": str(payload.get("title") or sid),
            "subtitle": "installed schedule", "resource": sid,
            "position": {"x": -280, "y": 210 + index * 166},
            "config": {k: schedule[k] for k in ("id", "kind", "cron", "every_seconds", "enabled", "paused", "session_kind", "session_mode", "target", "payload") if k in schedule},
            "binding": {"file": None, "path": None}, "editable": False,
            "href": f"/workflows?schedule={quote(sid, safe='')}",
        })
        has_agent = any(n["id"] == "agent:runtime" for n in graph["nodes"])
        target = "agent:runtime" if schedule.get("session_kind") == "agent" and has_agent else next((n["id"] for n in graph["nodes"] if n["kind"] == "strategy"), "")
        graph["edges"].append({"id": f"installed:{sid}", "source": nid, "target": target,
                               "relation": "installed_trigger", "origin": "runtime", "label": "installed_trigger"})
        index += 1


def workflow_index(paths: WorkspacePaths) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    sources: list[tuple[str, str | None]] = []
    if paths.strategies.exists():
        sources.extend((d.name, None) for d in sorted(paths.strategies.iterdir())
                       if d.is_dir() and not d.is_symlink() and (d / "strategy.yml").exists())
    proposals = list_proposals(paths)
    proposal_index = {proposal.id: proposal for proposal in proposals}
    for proposal in proposals:
        if proposal.kind not in {"strategy_package_proposal", "strategy_tuning_proposal"} or proposal.state in {"applied", "rejected", "superseded", "rolled_back"}:
            continue
        after = proposal.path / "after" / "strategies"
        if after.exists() and not after.is_symlink():
            sources.extend((d.name, proposal.id) for d in sorted(after.iterdir()) if d.is_dir() and not d.is_symlink())
    for strategy_id, proposal_id in sources:
        row: dict[str, Any] = {"key": f"{strategy_id}:{proposal_id or 'published'}", "strategy_id": strategy_id, "proposal_id": proposal_id}
        try:
            out = view_workflow(paths, strategy_id, proposal_id, _proposal_index=proposal_index)
            manifest = out["manifest"]
            row.update(title=str(manifest.get("title") or strategy_id), description=str(manifest.get("description") or ""),
                       mode=str(manifest.get("mode") or "paper"), state=out["source"]["state"],
                       status=str(manifest.get("status") or manifest.get("mode") or "draft"),
                       execution_mode=str(manifest.get("execution_mode") or manifest.get("driver") or "script"),
                       counts={kind: sum(n["kind"] == kind for n in out["strategy"]["nodes"]) for kind in ("source", "script", "agent", "scheduler", "account")},
                       markets=manifest.get("markets", []), legacy=out["legacy"], revision=out["revision"])
        except (WorkflowError, OSError, ValueError) as exc:
            row.update(title=strategy_id, state="invalid", mode="unknown", counts={}, error=str(exc))
        rows.append(row)
    return {"ok": True, "workflows": rows, "total": len(rows)}


def _set_path(root: dict[str, Any], path: list[str | int], value: Any) -> None:
    current: Any = root
    for index, key in enumerate(path[:-1]):
        if isinstance(current, dict) and key not in current:
            current[key] = [] if isinstance(path[index + 1], int) else {}
        try:
            current = current[key]
        except (KeyError, IndexError, TypeError) as exc:
            raise WorkflowError("Resource changed; reload the workflow") from exc
    try:
        current[path[-1]] = copy.deepcopy(value)
    except (IndexError, TypeError) as exc:
        raise WorkflowError("Resource changed; reload the workflow") from exc


def _apply_node_change(files: dict[str, str], manifest: dict[str, Any], node: dict[str, Any], change: dict[str, Any]) -> None:
    binding = node["binding"]
    if "content" in change:
        path = binding.get("file")
        if not path or not node["editable"]:
            raise WorkflowError("This node has no editable package file")
        content = change["content"]
        if not isinstance(content, str) or len(content.encode()) > MAX_FILE_BYTES:
            raise WorkflowError("File content is invalid or too large")
        files[safe_relative_path(path)] = content
    if "config" not in change:
        return
    path = binding.get("path")
    value = change["config"]
    if path is None or not node["editable"]:
        raise WorkflowError("System gates and external resources must be edited at their owning surface")
    if path == []:
        if not isinstance(value, dict) or set(value) - {"title", "description"}:
            raise WorkflowError("Strategy card accepts title and description only; lifecycle gates are separate")
        manifest.update(value)
    elif path == ["$agent"]:
        if not isinstance(value, dict) or set(value) - {"agent_session", "agent_profile", "llm_policy", "agent_execution", "agent_context"}:
            raise WorkflowError("Invalid Agent configuration")
        manifest.update(value)
    elif path == ["$tuning"]:
        if not isinstance(value, dict) or set(value) - {"objectives", "proposal_policy", "tuning_prompt"}:
            raise WorkflowError("Invalid review proposal configuration")
        manifest.setdefault("tuning", {}).update(value)
    else:
        if path == ["tuning", "guardrails"] and isinstance(value, dict) and value.get("require_operator_approval") is False:
            raise WorkflowError("Workflow edits cannot disable operator approval")
        _set_path(manifest, path, value)


def _add_resource(files: dict[str, str], manifest: dict[str, Any], addition: dict[str, Any]) -> None:
    kind, name = addition.get("kind"), str(addition.get("name") or "").strip()
    if not name or len(name) > 128:
        raise WorkflowError("A resource needs a name of at most 128 characters")
    if kind in {"script", "agent"}:
        if kind == "agent" and not _ID.fullmatch(name):
            raise WorkflowError("Agent name must be an identifier")
        path = f"subagents/{name}.agent.md" if kind == "agent" else safe_relative_path(name)
        if kind == "script" and (not path.endswith(".py") or path.split("/")[0] in _SKIP_DIRS or path == "main.py"):
            raise WorkflowError("Add a package-local Python helper, not an entrypoint replacement")
        if path in files:
            raise WorkflowError("Resource already exists")
        content = addition.get("content")
        if not isinstance(content, str) or len(content.encode()) > MAX_FILE_BYTES:
            raise WorkflowError("Resource content is required and must fit the file limit")
        files[path] = content
        if kind == "agent":
            manifest.setdefault("subagents", []).append(name)
    elif kind == "account":
        accounts = manifest.setdefault("accounts", [])
        if name in accounts:
            raise WorkflowError("Account reference already exists")
        accounts.append(name)
    elif kind == "source":
        value = addition.get("config")
        if not isinstance(value, dict):
            raise WorkflowError("Data source configuration must be an object")
        sources = manifest.setdefault("data_sources", [])
        if isinstance(sources, dict):
            if name in sources:
                raise WorkflowError("Data source already exists")
            sources[name] = {**value, "id": name}
        else:
            if any(isinstance(s, dict) and s.get("id") == name for s in sources):
                raise WorkflowError("Data source already exists")
            sources.append({**value, "id": name})
    else:
        raise WorkflowError("Unsupported resource kind")


def propose_workflow(paths: WorkspacePaths, payload: dict[str, Any]) -> dict[str, Any]:
    """Compare-and-propose. The active package is never modified here."""
    from .validator import validate_proposal_files

    strategy_id = str(payload.get("strategy_id") or "")
    proposal_id = str(payload.get("proposal_id") or "") or None
    with _LOCK:
        files, source = source_files(paths, strategy_id, proposal_id)
        if source["omitted_files"]:
            raise WorkflowError("Package contains unreadable/oversized files; use the existing file-authoring tools")
        revision = package_revision(files)
        if payload.get("base_revision") != revision:
            raise WorkflowError("revision_conflict: package changed; reload before saving")
        view = build_workflows(files)
        if view["legacy"]:
            raise WorkflowError("Legacy strategy: use its existing detail editor before migrating to a runtime package")
        manifest = copy.deepcopy(view["manifest"])
        canonical = {n["id"]: n for g in (view["strategy"], view["evolution"]) for n in g["nodes"]}
        changes, additions = payload.get("changes", []), payload.get("additions", [])
        if not isinstance(changes, list) or not isinstance(additions, list) or len(changes) + len(additions) > 100:
            raise WorkflowError("At most 100 resource changes per proposal")
        for change in changes:
            if not isinstance(change, dict) or change.get("node_id") not in canonical:
                raise WorkflowError("Unknown workflow node")
            _apply_node_change(files, manifest, canonical[change["node_id"]], change)
        for addition in additions:
            if not isinstance(addition, dict):
                raise WorkflowError("Invalid resource addition")
            _add_resource(files, manifest, addition)
        files["strategy.yml"] = yaml_io.dumps(manifest)
        if "metadata" in payload:
            metadata = payload["metadata"]
            if not isinstance(metadata, dict):
                raise WorkflowError("Workflow metadata must be an object")
            files[WORKFLOW_FILE] = json.dumps(metadata, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        build_workflows(files)
        validation = validate_proposal_files(strategy_id=strategy_id, files=files)
        if not validation.ok:
            return {"ok": False, "error": "validation_failed", "validation": validation.asdict()}
        # Recheck after validation: tools outside this service may edit files.
        latest, _ = source_files(paths, strategy_id, proposal_id)
        if package_revision(latest) != revision:
            raise WorkflowError("revision_conflict: package changed during validation")
        proposal = create_proposal(
            paths, kind="strategy_package_proposal", summary=f"Workflow · {manifest.get('title') or strategy_id}",
            rationale="Operator-authored workflow edits. Existing executable source remains authoritative; annotations do not execute.",
            test_plan="Static/SDK validation passed. Review the diff and run required backtests before promotion.",
            rollback="The current strategy is unchanged until approved promotion; use version rollback after promotion.",
            target=f"strategies/{strategy_id}", initial_state="pending_review",
            extra_files={**{f"after/strategies/{strategy_id}/{rel}": text for rel, text in files.items()},
                         "validation_report.json": json.dumps(validation.asdict(), indent=2)},
            metadata={"strategy_id": strategy_id, "workflow_edit": True, "source_proposal_id": proposal_id, "base_revision": revision},
        )
        return {"ok": True, "strategy_id": strategy_id, "proposal_id": proposal.id,
                "state": proposal.state, "validation": validation.asdict(),
                "workflow": view_workflow(paths, strategy_id, proposal.id)}
