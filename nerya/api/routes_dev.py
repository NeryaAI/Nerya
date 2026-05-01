"""Dev-mode introspection routes.

Available when ``runtime.dev_mode: true`` or ``NERYA_DEV_MODE=1``. These
endpoints expose the recorder so the dashboard and e2e tests can show
exactly which HTTP calls, tool invocations, and errors happened during
the last turn.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..core import devmode
from ..sdk import InternalClient


def _status(client: InternalClient, _params: dict[str, Any]) -> dict[str, Any]:
    root = client.config.paths.dev_logs
    files = []
    if root.exists():
        for f in sorted(root.glob("*.jsonl")):
            files.append({"name": f.name, "size": f.stat().st_size})
    return {
        "active": devmode.is_active(),
        "env_flag": bool(os.environ.get("NERYA_DEV_MODE")),
        "config_flag": bool(client.config.get("runtime.dev_mode")),
        "dir": str(root),
        "files": files,
    }


def _tail(client: InternalClient, params: dict[str, Any]) -> dict[str, Any]:
    kind = str(params.get("kind") or "http")
    try:
        limit = int(params.get("limit") or 50)
    except (TypeError, ValueError):
        limit = 50
    path: Path = client.config.paths.dev_log(kind)
    if not path.exists():
        return {"kind": kind, "items": [], "path": str(path)}
    lines = path.read_text(encoding="utf-8").splitlines()
    tail = lines[-max(1, limit) :]
    items: list[Any] = []
    for line in tail:
        try:
            items.append(json.loads(line))
        except Exception:
            items.append({"_raw": line})
    return {"kind": kind, "items": items, "path": str(path)}


def _recent(client: InternalClient, params: dict[str, Any]) -> dict[str, Any]:
    kind = str(params.get("kind") or "http")
    try:
        limit = int(params.get("limit") or 50)
    except (TypeError, ValueError):
        limit = 50
    try:
        rec = devmode.get_recorder(client.config.paths)
        events = rec.recent(kind=kind, limit=limit)
    except Exception as exc:  # pragma: no cover - recorder is tolerant
        return {"kind": kind, "items": [], "error": str(exc)}
    return {
        "kind": kind,
        "items": [
            {"kind": e.kind, "at": e.at, "data": e.data} for e in events
        ],
    }


def _clear(client: InternalClient, _params: dict[str, Any]) -> dict[str, Any]:
    root = client.config.paths.dev_logs
    removed = 0
    if root.exists():
        for f in root.glob("*.jsonl"):
            f.unlink()
            removed += 1
    return {"removed": removed, "dir": str(root)}


def routes():
    return [
        ("GET", "/dev/status", _status),
        ("GET", "/dev/tail", _tail),
        ("GET", "/dev/recent", _recent),
        ("POST", "/dev/clear", _clear),
    ]
