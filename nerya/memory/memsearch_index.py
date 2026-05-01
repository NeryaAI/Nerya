"""Optional memsearch-backed semantic index for Nerya memory.

The markdown files under ``memory/`` and strategy ``learnings.md`` files stay
the source of truth. memsearch is a derived, rebuildable index that is disabled
by default and only installed / started after an explicit operator action.
"""

from __future__ import annotations

import asyncio
import importlib.util
import importlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..core import yaml_io
from ..core.config import Config


_WATCHER_PROCESS: subprocess.Popen | None = None


def _cfg(config: Config) -> dict[str, Any]:
    raw = config.get("memory.vector_search", {}) or {}
    return dict(raw) if isinstance(raw, dict) else {}


def _relative_path(root: Path, raw: str) -> Path:
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = root / p
    return p.resolve()


def source_paths(config: Config) -> list[Path]:
    cfg = _cfg(config)
    configured = cfg.get("paths") or ["memory", "strategies"]
    paths: list[Path] = []
    for raw in configured:
        text = str(raw or "").strip()
        if not text:
            continue
        paths.append(_relative_path(config.paths.root, text))
    if not paths:
        paths = [config.paths.memory, config.paths.strategies]
    return paths


def dependency_available() -> bool:
    return importlib.util.find_spec("memsearch") is not None


def status(config: Config) -> dict[str, Any]:
    cfg = _cfg(config)
    process_running = bool(_WATCHER_PROCESS and _WATCHER_PROCESS.poll() is None)
    return {
        "ok": True,
        "enabled": bool(cfg.get("enabled", False)),
        "backend": cfg.get("backend") or "memsearch",
        "dependency_available": dependency_available(),
        "install_package": cfg.get("install_package") or "memsearch",
        "watch_enabled": bool(cfg.get("watch_enabled", False)),
        "watcher_running": process_running,
        "paths": [str(p) for p in source_paths(config)],
    }


def configure(
    config: Config,
    *,
    enabled: bool | None = None,
    watch_enabled: bool | None = None,
    paths: list[str] | None = None,
    install_package: str | None = None,
) -> dict[str, Any]:
    existing = yaml_io.load(config.paths.config, default={}) or {}
    if not isinstance(existing, dict):
        existing = {}
    memory = existing.setdefault("memory", {})
    if not isinstance(memory, dict):
        memory = {}
        existing["memory"] = memory
    vector = memory.setdefault("vector_search", {})
    if not isinstance(vector, dict):
        vector = {}
        memory["vector_search"] = vector

    vector.setdefault("backend", "memsearch")
    vector.setdefault("install_package", "memsearch")
    vector.setdefault("paths", ["memory", "strategies"])
    if enabled is not None:
        vector["enabled"] = bool(enabled)
    if watch_enabled is not None:
        vector["watch_enabled"] = bool(watch_enabled)
    if paths is not None:
        cleaned = [str(p).strip() for p in paths if str(p or "").strip()]
        vector["paths"] = cleaned or ["memory", "strategies"]
    if install_package:
        vector["install_package"] = install_package

    yaml_io.dump(config.paths.config, existing)
    config.data.setdefault("memory", {})
    if not isinstance(config.data["memory"], dict):
        config.data["memory"] = {}
    config.data["memory"]["vector_search"] = vector
    return status(config)


def install_dependency(config: Config) -> dict[str, Any]:
    cfg = _cfg(config)
    if not bool(cfg.get("enabled", False)):
        return {
            "ok": False,
            "error": "vector_search_disabled",
            "detail": "Enable memory.vector_search before installing memsearch.",
        }
    package = str(cfg.get("install_package") or "memsearch").strip()
    if not package:
        package = "memsearch"
    cmd = [sys.executable, "-m", "pip", "install", package]
    proc = subprocess.run(
        cmd,
        cwd=str(config.paths.root),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    importlib.invalidate_caches()
    return {
        "ok": proc.returncode == 0,
        "cmd": cmd,
        "returncode": proc.returncode,
        "stdout_tail": proc.stdout[-4000:],
        "stderr_tail": proc.stderr[-4000:],
        "dependency_available": dependency_available(),
    }


def _require_ready(config: Config) -> tuple[bool, dict[str, Any] | None]:
    cfg = _cfg(config)
    if not bool(cfg.get("enabled", False)):
        return False, {"ok": False, "error": "vector_search_disabled"}
    if str(cfg.get("backend") or "memsearch") != "memsearch":
        return False, {"ok": False, "error": "unsupported_backend"}
    if not dependency_available():
        return False, {
            "ok": False,
            "error": "dependency_missing",
            "install_package": cfg.get("install_package") or "memsearch",
        }
    return True, None


async def _index_async(paths: list[Path], *, force: bool = False) -> Any:
    from memsearch import MemSearch  # type: ignore

    mem = MemSearch(paths=[str(p) for p in paths if p.exists()])
    try:
        return await mem.index(force=force)
    except TypeError:
        return await mem.index()


async def _search_async(paths: list[Path], query: str, *, top_k: int) -> Any:
    from memsearch import MemSearch  # type: ignore

    mem = MemSearch(paths=[str(p) for p in paths if p.exists()])
    return await mem.search(query, top_k=top_k)


def reindex(config: Config, *, force: bool = False) -> dict[str, Any]:
    ready, error = _require_ready(config)
    if not ready:
        return error or {"ok": False}
    paths = source_paths(config)
    result = asyncio.run(_index_async(paths, force=force))
    return {
        "ok": True,
        "indexed": True,
        "force": bool(force),
        "paths": [str(p) for p in paths],
        "result": result,
    }


def search(config: Config, *, query: str, top_k: int = 5) -> dict[str, Any]:
    ready, error = _require_ready(config)
    if not ready:
        return error or {"ok": False}
    clean = str(query or "").strip()
    if not clean:
        return {"ok": False, "error": "query_required"}
    rows = asyncio.run(_search_async(source_paths(config), clean, top_k=max(1, top_k)))
    return {"ok": True, "query": clean, "results": rows, "count": len(rows or [])}


def start_watcher(config: Config) -> dict[str, Any]:
    global _WATCHER_PROCESS
    ready, error = _require_ready(config)
    if not ready:
        return error or {"ok": False}
    if _WATCHER_PROCESS and _WATCHER_PROCESS.poll() is None:
        return {**status(config), "started": False, "detail": "already_running"}
    paths = [str(p) for p in source_paths(config) if p.exists()]
    if not paths:
        return {"ok": False, "error": "no_existing_source_paths"}
    log_path = config.paths.dev_log("memsearch_watch")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    code = (
        "import asyncio, json\n"
        "from memsearch import MemSearch\n"
        f"paths = {json.dumps(paths)}\n"
        "asyncio.run(MemSearch(paths=paths).watch())\n"
    )
    log = log_path.open("ab")
    _WATCHER_PROCESS = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=str(config.paths.root),
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    configure(config, watch_enabled=True)
    return {
        **status(config),
        "started": True,
        "pid": _WATCHER_PROCESS.pid,
        "log_path": str(log_path),
    }


def stop_watcher(config: Config) -> dict[str, Any]:
    global _WATCHER_PROCESS
    stopped = False
    if _WATCHER_PROCESS and _WATCHER_PROCESS.poll() is None:
        _WATCHER_PROCESS.terminate()
        stopped = True
    _WATCHER_PROCESS = None
    configure(config, watch_enabled=False)
    return {**status(config), "stopped": stopped}
