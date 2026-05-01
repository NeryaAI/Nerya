"""Strategy CRUD helpers for the dashboard.

The legacy ``strategy`` skill exposed ``list`` / ``get`` / ``create`` /
``update`` / ``set_status`` / ``bind_wallet`` / ``bind_account`` /
``resolve_runtime`` / ``versions`` actions through the
``/skills/call`` bridge. That skill was archived during the workspace-
native rewrite, but the dashboard still expects the same surface.

This module reimplements those operations directly against the on-disk
package layout (``workspace/strategies/<id>/``) so the dashboard can
reach them through plain REST routes without going back through the
legacy skill bridge.

On-disk shape (single source of truth):

    strategies/<id>/
    ├── strategy.yml       # manifest (id, title, status, mode, markets, ...)
    ├── config.yml         # operational config the strategy reads via ctx.config
    ├── limits.yml         # per-strategy trading limits
    ├── prompts/
    │   ├── main.md
    │   └── <role>.md      # other prompt files (subagent overrides, etc.)
    ├── learnings.md       # appendable strategy memory
    └── versions/
        └── <ts>__<reason>/  # snapshot of strategy.yml/config.yml/limits.yml

Writes are best-effort atomic (``.tmp`` + ``replace``) so a half-written
file never breaks the dashboard listing.

The CRUD layer is intentionally thin — risk gating, approval flow,
schedule installation, etc. all stay in their existing modules.
Callers (HTTP routes, MCP, CLI) layer their own auth on top.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ..core import yaml_io
from ..core.errors import TradingError
from ..core.paths import WorkspacePaths
from ..core.time import now_iso
from .strategies import (
    Strategy,
    list_strategies as _list_strategies,
    load_strategy as _load_strategy,
)
from .strategy_lifecycle import (
    InvalidTransition,
    STATES,
    validate_transition,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _read_yaml(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default if default is not None else {}
    try:
        return yaml_io.load(path, default=default if default is not None else {})
    except Exception:
        return default if default is not None else {}


def _write_yaml(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    yaml_io.dump(path, data)


def _read_prompts(prompt_dir: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not prompt_dir.is_dir():
        return out
    for p in sorted(prompt_dir.iterdir()):
        if p.suffix.lower() not in {".md", ".txt"}:
            continue
        try:
            out[p.stem] = p.read_text(encoding="utf-8")
        except Exception:
            continue
    return out


def _write_prompts(prompt_dir: Path, prompts: dict[str, str]) -> list[str]:
    """Write each ``name -> body`` to ``prompt_dir/<name>.md``.

    Returns the list of stems actually changed (created or overwritten).
    Empty bodies are written as empty files so the operator can tell
    "no prompt yet" from "deleted on purpose" via filesystem signals.
    """

    prompt_dir.mkdir(parents=True, exist_ok=True)
    changed: list[str] = []
    for raw_name, body in (prompts or {}).items():
        name = str(raw_name).strip()
        if not name:
            continue
        # Don't allow path escapes.
        if "/" in name or "\\" in name or name.startswith("."):
            continue
        path = prompt_dir / f"{name}.md"
        text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        prev = path.read_text(encoding="utf-8") if path.exists() else None
        if prev != text:
            _atomic_write_text(path, text)
            changed.append(name)
    return changed


def _strategy_record(s: Strategy, *, mode_hint: Optional[str] = None) -> dict[str, Any]:
    """Render a :class:`Strategy` into the dashboard's ``StrategyRecord`` shape."""

    yml = _read_yaml(s.path / "strategy.yml")
    mode = mode_hint or str(
        yml.get("mode")
        or ("live" if s.live_trading_enabled else "paper")
    )
    return {
        "id": s.id,
        "title": s.title,
        "status": s.status,
        "mode": mode,
        "enabled": bool(s.is_tradable),
        "account_id": s.account_id,
        "wallet_id": yml.get("wallet_id") or None,
        "markets": list(s.markets),
        "subagents": list(s.subagents),
        "trigger_kinds": list(s.trigger_kinds),
        "path": str(s.path),
    }


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def list_records(
    paths: WorkspacePaths,
    *,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for s in _list_strategies(paths):
        if not include_archived and s.status == "archived":
            continue
        out.append(_strategy_record(s))
    return out


def get_detail(paths: WorkspacePaths, strategy_id: str) -> dict[str, Any]:
    """Return the full strategy detail consumed by the dashboard.

    Shape mirrors ``StrategyDetail`` in ``dashboard/lib/clientApi.ts``:
    ``{ strategy, strategy_yml, config, limits, prompts, learnings }``.
    """

    s = _load_strategy(paths, strategy_id)
    yml = _read_yaml(s.path / "strategy.yml")
    cfg = _read_yaml(s.path / "config.yml")
    limits = _read_yaml(s.path / "limits.yml")
    prompts = _read_prompts(s.path / "prompts")
    learnings = ""
    learnings_path = s.path / "learnings.md"
    if learnings_path.exists():
        try:
            learnings = learnings_path.read_text(encoding="utf-8")
        except Exception:
            learnings = ""
    return {
        "strategy": _strategy_record(s),
        "strategy_yml": yml,
        "config": cfg if isinstance(cfg, dict) else {},
        "limits": limits if isinstance(limits, dict) else {},
        "prompts": prompts,
        "learnings": learnings,
    }


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@dataclass
class CreateRequest:
    strategy_id: str
    title: str
    description: str = ""
    account_id: str = "paper_main"
    markets: tuple[str, ...] = ()
    trigger_kinds: tuple[str, ...] = ()
    subagents: tuple[str, ...] = ()
    driver: str = "prompt"
    status: str = "draft"
    wallet_id: Optional[str] = None
    main_prompt: str = ""


def create(paths: WorkspacePaths, req: CreateRequest) -> dict[str, Any]:
    sid = (req.strategy_id or "").strip().lower()
    if not _ID_RE.match(sid):
        raise TradingError(
            f"invalid strategy_id: {sid!r} (use lowercase letters, digits, "
            "_, -, 2-64 chars)"
        )
    if req.status not in STATES:
        raise TradingError(f"invalid status: {req.status!r}")
    root = paths.strategy(sid)
    if root.exists() and (root / "strategy.yml").exists():
        raise TradingError(f"strategy already exists: {sid}")
    root.mkdir(parents=True, exist_ok=True)

    title = (req.title or sid).strip()
    manifest: dict[str, Any] = {
        "id": sid,
        "strategy_id": sid,
        "title": title,
        "description": req.description or "",
        "status": req.status,
        "mode": "paper" if req.status == "paper" else "paper",
        "driver": req.driver or "prompt",
        "account_id": req.account_id or "paper_main",
        "markets": list(req.markets),
        "trigger_kinds": list(req.trigger_kinds),
        "subagents": list(req.subagents),
        "paper_trading_enabled": True,
        "live_trading_enabled": False,
    }
    if req.wallet_id:
        manifest["wallet_id"] = req.wallet_id

    _write_yaml(root / "strategy.yml", manifest)
    _write_yaml(root / "config.yml", {})
    _write_yaml(root / "limits.yml", {
        "allowed_markets": list(req.markets),
        "max_single_order_usd": 0,
        "max_total_exposure_usd": 0,
        "daily_loss_usd": 0,
        "max_drawdown_pct": 0,
        "min_confidence": 0.5,
        "max_slippage_bps": 50,
        "max_stale_seconds": 30,
        "approval_threshold_usd": 0,
        "kill_switch": False,
    })
    if req.main_prompt:
        _write_prompts(root / "prompts", {"main": req.main_prompt})
    return {
        "ok": True,
        "strategy_id": sid,
        "state": req.status,
        "path": str(root),
    }


# ---------------------------------------------------------------------------
# Versions / snapshots
# ---------------------------------------------------------------------------


def _versions_dir(strategy_root: Path) -> Path:
    return strategy_root / "versions"


def _snapshot(strategy_root: Path, *, reason: str) -> str:
    """Snapshot strategy.yml/config.yml/limits.yml/prompts under versions/.

    Returns the version id (timestamped folder name). Cheap; bounded by
    however many ticks the operator does.
    """

    ts = now_iso().replace(":", "-")
    safe_reason = re.sub(r"[^a-z0-9_]+", "_", reason.lower())[:32] or "edit"
    vid = f"{ts}__{safe_reason}"
    vdir = _versions_dir(strategy_root) / vid
    vdir.mkdir(parents=True, exist_ok=True)
    for name in ("strategy.yml", "config.yml", "limits.yml"):
        src = strategy_root / name
        if src.exists():
            shutil.copy2(src, vdir / name)
    prompts_dir = strategy_root / "prompts"
    if prompts_dir.is_dir():
        dst_prompts = vdir / "prompts"
        dst_prompts.mkdir(parents=True, exist_ok=True)
        for p in prompts_dir.iterdir():
            if p.is_file():
                shutil.copy2(p, dst_prompts / p.name)
    return vid


def versions(paths: WorkspacePaths, strategy_id: str) -> dict[str, Any]:
    s = _load_strategy(paths, strategy_id)
    vdir = _versions_dir(s.path)
    if not vdir.is_dir():
        return {"strategy_id": strategy_id, "versions": []}
    rows: list[dict[str, Any]] = []
    for entry in sorted(vdir.iterdir(), reverse=True):
        if not entry.is_dir():
            continue
        manifest = _read_yaml(entry / "strategy.yml")
        rows.append({
            "version_id": entry.name,
            "ts": entry.name.split("__", 1)[0],
            "reason": entry.name.split("__", 1)[1] if "__" in entry.name else "",
            "title": manifest.get("title") if isinstance(manifest, dict) else None,
            "status": manifest.get("status") if isinstance(manifest, dict) else None,
        })
    return {"strategy_id": strategy_id, "versions": rows}


# ---------------------------------------------------------------------------
# Update / set_status / bind_*
# ---------------------------------------------------------------------------


_PATCHABLE_FIELDS = {
    "title", "description", "account_id", "wallet_id", "markets",
    "trigger_kinds", "subagents", "driver",
}


def update(
    paths: WorkspacePaths,
    strategy_id: str,
    *,
    patch: dict[str, Any],
    reason: str = "dashboard_update",
) -> dict[str, Any]:
    s = _load_strategy(paths, strategy_id)
    yml = _read_yaml(s.path / "strategy.yml")
    if not isinstance(yml, dict):
        yml = {}
    changed: list[str] = []

    for field in _PATCHABLE_FIELDS:
        if field not in patch:
            continue
        new_val = patch.get(field)
        if field in {"markets", "trigger_kinds", "subagents"}:
            if not isinstance(new_val, list):
                continue
            new_val = [str(v) for v in new_val if v]
        if yml.get(field) != new_val:
            yml[field] = new_val
            changed.append(field)

    cfg_in = patch.get("config")
    if isinstance(cfg_in, dict):
        prev = _read_yaml(s.path / "config.yml")
        if prev != cfg_in:
            _write_yaml(s.path / "config.yml", cfg_in)
            changed.append("config")

    limits_in = patch.get("limits")
    if isinstance(limits_in, dict):
        prev = _read_yaml(s.path / "limits.yml")
        if prev != limits_in:
            _write_yaml(s.path / "limits.yml", limits_in)
            changed.append("limits")

    prompts_in = patch.get("prompts")
    if isinstance(prompts_in, dict):
        touched = _write_prompts(s.path / "prompts", {
            k: v for k, v in prompts_in.items() if isinstance(v, str)
        })
        if touched:
            changed.append("prompts")

    version_id: Optional[str] = None
    if changed:
        # Write a snapshot *before* we overwrite strategy.yml so the
        # snapshot reflects pre-edit state — gives the operator the
        # equivalent of "git diff before save" for free.
        try:
            version_id = _snapshot(s.path, reason=reason or "dashboard_update")
        except Exception:
            version_id = None
        # Persist manifest changes after the snapshot.
        if any(f in _PATCHABLE_FIELDS for f in changed):
            _write_yaml(s.path / "strategy.yml", yml)

    return {
        "ok": True,
        "strategy_id": strategy_id,
        "changed": changed,
        "version_id": version_id,
    }


def set_status(
    paths: WorkspacePaths,
    strategy_id: str,
    status: str,
    *,
    reason: str = "dashboard_update",
) -> dict[str, Any]:
    s = _load_strategy(paths, strategy_id)
    if status not in STATES:
        raise TradingError(f"invalid status: {status!r}")
    try:
        validate_transition(s.status, status)
    except InvalidTransition as exc:
        raise TradingError(f"invalid status transition: {exc}") from exc

    yml = _read_yaml(s.path / "strategy.yml")
    if not isinstance(yml, dict):
        yml = {}
    if yml.get("status") == status:
        return {
            "ok": True, "strategy_id": strategy_id, "status": status,
            "changed": False,
        }
    try:
        _snapshot(s.path, reason=f"status_{status}_{reason or 'dashboard'}")
    except Exception:
        pass
    yml["status"] = status
    yml["mode"] = (
        "live" if status in {"canary", "live"}
        else "paper" if status in {"draft", "paper", "paused"}
        else yml.get("mode", "paper")
    )
    yml["paper_trading_enabled"] = status in {"draft", "paper", "paused", "canary"}
    yml["live_trading_enabled"] = status in {"canary", "live"}
    _write_yaml(s.path / "strategy.yml", yml)
    return {
        "ok": True, "strategy_id": strategy_id, "status": status,
        "changed": True,
    }


def bind_wallet(
    paths: WorkspacePaths,
    strategy_id: str,
    wallet_id: Optional[str],
) -> dict[str, Any]:
    s = _load_strategy(paths, strategy_id)
    yml = _read_yaml(s.path / "strategy.yml")
    if not isinstance(yml, dict):
        yml = {}
    if wallet_id:
        yml["wallet_id"] = str(wallet_id)
    else:
        yml.pop("wallet_id", None)
    _write_yaml(s.path / "strategy.yml", yml)
    return {
        "ok": True, "strategy_id": strategy_id,
        "wallet_id": wallet_id or None,
    }


def bind_account(
    paths: WorkspacePaths,
    strategy_id: str,
    account_id: str,
) -> dict[str, Any]:
    s = _load_strategy(paths, strategy_id)
    yml = _read_yaml(s.path / "strategy.yml")
    if not isinstance(yml, dict):
        yml = {}
    yml["account_id"] = str(account_id)
    _write_yaml(s.path / "strategy.yml", yml)
    return {
        "ok": True, "strategy_id": strategy_id,
        "account_id": str(account_id),
    }


def resolve_runtime(
    paths: WorkspacePaths,
    strategy_id: str,
) -> dict[str, Any]:
    """Mirror the legacy ``strategy.resolve_runtime`` action.

    Returns the effective account + wallet bindings the strategy will
    use at runtime, plus the source of each binding (``strategy.yml``
    vs workspace defaults). Used by the dashboard to show "this
    strategy will trade on <account>" before the operator hits run.
    """

    s = _load_strategy(paths, strategy_id)
    yml = _read_yaml(s.path / "strategy.yml")
    yml = yml if isinstance(yml, dict) else {}
    wallet_id = yml.get("wallet_id")
    return {
        "ok": True,
        "strategy_id": strategy_id,
        "effective_account": s.account_id,
        "account_source": "strategy.yml",
        "effective_wallet": wallet_id or None,
        "wallet_source": "strategy.yml" if wallet_id else "global",
    }


# ---------------------------------------------------------------------------
# Package file CRUD (main.py / strategy.md / subagents/*.agent.md / tests/*)
# ---------------------------------------------------------------------------

# Files outside this allowlist (relative to the strategy root) cannot be
# read or written through the dashboard — they're either generated by
# the runtime (``runs/``, ``logs/``, ``state/``, ``versions/``) or
# managed via the dedicated CRUD endpoints (``strategy.yml``,
# ``config.yml``, ``limits.yml``, ``prompts/``).
_PACKAGE_FILE_PREFIXES: tuple[str, ...] = (
    "main.py",
    "strategy.md",
    "strategy.yml",
    "config.yml",
    "limits.yml",
    "subagents/",
    "tests/",
    "fixtures/",
    "prompts/",
    "learnings.md",
    "README.md",
)

# Hard cap on a single file's size so the dashboard can't accidentally
# ship a binary blob at the operator. Anything larger is excluded
# from the listing and rejected on write.
_MAX_PACKAGE_FILE_BYTES: int = 1_000_000


def _is_allowed_package_path(rel: str) -> bool:
    rel = rel.replace("\\", "/").lstrip("/")
    if not rel or rel.startswith(".") or ".." in rel.split("/"):
        return False
    return any(rel == p or rel.startswith(p) for p in _PACKAGE_FILE_PREFIXES)


def list_files(paths: WorkspacePaths, strategy_id: str) -> dict[str, Any]:
    """List package files under ``strategies/<id>/``.

    Returns the relative path, byte size, and a content snippet (first
    ~40KB) for each editable file. Larger files are listed without
    content so the operator can still see the file exists; they need
    a real diff tool for those.
    """

    s = _load_strategy(paths, strategy_id)
    root = s.path
    out: list[dict[str, Any]] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if not _is_allowed_package_path(rel):
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        entry: dict[str, Any] = {
            "rel_path": rel,
            "size": size,
            "kind": _infer_kind(rel),
        }
        if size <= _MAX_PACKAGE_FILE_BYTES:
            try:
                entry["content"] = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                entry["content"] = None
                entry["error"] = "decode_failed"
        else:
            entry["content"] = None
            entry["error"] = "too_large"
        out.append(entry)
    return {"strategy_id": strategy_id, "root": str(root), "files": out}


def _infer_kind(rel: str) -> str:
    if rel.endswith(".py"):
        return "python"
    if rel.endswith((".yml", ".yaml")):
        return "yaml"
    if rel.endswith(".md") or rel.endswith(".agent.md"):
        return "markdown"
    if rel.endswith(".json"):
        return "json"
    return "text"


def write_file(
    paths: WorkspacePaths,
    strategy_id: str,
    *,
    rel_path: str,
    content: str,
    reason: str = "dashboard_write_file",
) -> dict[str, Any]:
    """Write ``content`` to ``strategies/<id>/<rel_path>``.

    Snapshots the strategy package (manifest + prompts + limits +
    config) before the write so the operator can roll back via the
    versions tab. The destination path must be inside the package and
    on the editable allowlist.
    """

    rel = (rel_path or "").replace("\\", "/").strip().lstrip("/")
    if not _is_allowed_package_path(rel):
        raise TradingError(f"refusing to write {rel_path!r}: outside the editable allowlist")
    if not isinstance(content, str):
        raise TradingError("content must be a string")
    if len(content.encode("utf-8")) > _MAX_PACKAGE_FILE_BYTES:
        raise TradingError(
            f"content exceeds the {_MAX_PACKAGE_FILE_BYTES} byte cap; "
            "split the file or edit it directly on disk"
        )

    s = _load_strategy(paths, strategy_id)
    target = (s.path / rel).resolve()
    root_resolved = s.path.resolve()
    try:
        target.relative_to(root_resolved)
    except ValueError:
        raise TradingError(f"refusing to write outside strategy root: {rel_path!r}")

    version_id: Optional[str] = None
    try:
        version_id = _snapshot(s.path, reason=reason or "dashboard_write_file")
    except Exception:
        version_id = None

    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(target, content)
    return {
        "ok": True,
        "strategy_id": strategy_id,
        "rel_path": rel,
        "size": len(content.encode("utf-8")),
        "version_id": version_id,
    }


__all__ = [
    "CreateRequest",
    "bind_account",
    "bind_wallet",
    "create",
    "get_detail",
    "list_files",
    "list_records",
    "resolve_runtime",
    "set_status",
    "update",
    "versions",
    "write_file",
]
