from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any


# Caps for the dashboard files browser. Identical safety posture to the
# native ``file_ops`` tool: read text only, refuse to surface huge or
# binary blobs, and block destructive ops on safety-critical config
# files (``.env``, ``accounts/*`` …) so a stray click on the drawer
# can't overwrite secrets.
_DEFAULT_PAGE = 500
_HARD_PAGE = 2000
_MAX_TEXT_BYTES = 256 * 1024  # editor cap
_HARD_FILE_BYTES = 1 * 1024 * 1024  # absolute ceiling for a single read


def _resolve(client, raw: str, *, must_exist: bool = False) -> Path:
    from ..tools.native.paths import resolve_workspace_path

    return resolve_workspace_path(
        raw,
        root=client.config.paths.root,
        must_exist=must_exist,
        default=".",
    )


def _is_sensitive(rel: str) -> bool:
    from ..tools.native.file_ops import is_sensitive_mutation_path

    return is_sensitive_mutation_path(rel)


def _rel(client, path: Path) -> str:
    from ..tools.native.paths import to_workspace_relative

    return to_workspace_relative(path, client.config.paths.root)


_BINARY_SNIFF_BYTES = 4096


def _looks_binary(blob: bytes) -> bool:
    if not blob:
        return False
    if b"\x00" in blob:
        return True
    # Treat anything with >30% non-text bytes as binary.
    text_chars = bytes(range(32, 127)) + b"\n\r\t\b\f"
    nontext = sum(1 for b in blob if b not in text_chars)
    return (nontext / len(blob)) > 0.3


def routes():
    def workspace_info(client, _p):
        paths = client.config.paths
        return {
            "root": str(paths.root),
            "live_trading_enabled": client.config.live_trading_enabled,
            "kill_switch": client.config.kill_switch(),
        }

    # ------------------------------------------------------------------
    # files browser
    # ------------------------------------------------------------------

    def files_list(client, payload):
        params = payload or {}
        raw = str(params.get("path") or ".")
        show_hidden = str(params.get("show_hidden") or "").lower() in {"1", "true", "yes"}

        try:
            target = _resolve(client, raw, must_exist=True)
        except FileNotFoundError as exc:
            return {"ok": False, "error": "not_found", "detail": str(exc), "path": raw}
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__, "detail": str(exc)}

        if not target.is_dir():
            return {
                "ok": False,
                "error": "not_a_directory",
                "detail": f"{_rel(client, target)} is not a directory",
            }

        entries: list[dict[str, Any]] = []
        try:
            with os.scandir(target) as it:
                rows = list(it)
        except PermissionError as exc:
            return {"ok": False, "error": "permission_denied", "detail": str(exc)}

        rows.sort(key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
        page_limit = min(int(params.get("limit") or _DEFAULT_PAGE), _HARD_PAGE)
        truncated = False

        for entry in rows:
            if not show_hidden and entry.name.startswith("."):
                continue
            if len(entries) >= page_limit:
                truncated = True
                break
            try:
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            kind = "dir" if entry.is_dir(follow_symlinks=False) else "file"
            entries.append({
                "name": entry.name,
                "kind": kind,
                "size": stat.st_size if kind == "file" else None,
                "mtime_ms": int(stat.st_mtime * 1000),
                "path": _rel(client, target / entry.name),
                "is_symlink": entry.is_symlink(),
            })

        breadcrumbs = []
        cur = target
        root = client.config.paths.root.resolve()
        while True:
            label = cur.name or root.name
            breadcrumbs.append({"name": label, "path": _rel(client, cur)})
            if cur.resolve() == root:
                break
            cur = cur.parent
        breadcrumbs.reverse()

        return {
            "ok": True,
            "path": _rel(client, target),
            "absolute": str(target),
            "root": str(root),
            "entries": entries,
            "truncated": truncated,
            "show_hidden": show_hidden,
            "breadcrumbs": breadcrumbs,
        }

    def files_read(client, payload):
        params = payload or {}
        raw = str(params.get("path") or "")
        if not raw:
            return {"ok": False, "error": "missing_path"}
        try:
            target = _resolve(client, raw, must_exist=True)
        except FileNotFoundError as exc:
            return {"ok": False, "error": "not_found", "detail": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__, "detail": str(exc)}

        if target.is_dir():
            return {"ok": False, "error": "is_directory"}

        try:
            size = target.stat().st_size
        except OSError as exc:
            return {"ok": False, "error": "stat_failed", "detail": str(exc)}

        if size > _HARD_FILE_BYTES:
            return {
                "ok": False,
                "error": "too_large",
                "detail": f"file is {size} bytes (cap {_HARD_FILE_BYTES})",
                "size": size,
            }

        try:
            blob = target.read_bytes()
        except PermissionError as exc:
            return {"ok": False, "error": "permission_denied", "detail": str(exc)}
        except OSError as exc:
            return {"ok": False, "error": "read_failed", "detail": str(exc)}

        binary = _looks_binary(blob[:_BINARY_SNIFF_BYTES])
        truncated = False
        if binary:
            return {
                "ok": True,
                "path": _rel(client, target),
                "binary": True,
                "size": size,
                "content": "",
                "truncated": False,
            }

        if size > _MAX_TEXT_BYTES:
            blob = blob[:_MAX_TEXT_BYTES]
            truncated = True

        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError:
            text = blob.decode("utf-8", errors="replace")

        return {
            "ok": True,
            "path": _rel(client, target),
            "binary": False,
            "size": size,
            "content": text,
            "truncated": truncated,
        }

    def files_write(client, payload):
        body = payload or {}
        raw = str(body.get("path") or "")
        content = body.get("content")
        if not raw:
            return {"ok": False, "error": "missing_path"}
        if not isinstance(content, str):
            return {"ok": False, "error": "invalid_content"}

        try:
            target = _resolve(client, raw)
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__, "detail": str(exc)}

        rel = _rel(client, target)
        if _is_sensitive(rel):
            return {
                "ok": False,
                "error": "sensitive_path",
                "detail": (
                    f"refusing to write {rel} from the dashboard (use the "
                    "settings UI for credentials/config)"
                ),
            }

        if target.is_dir():
            return {"ok": False, "error": "is_directory"}

        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.write_bytes(content.encode("utf-8"))
        except PermissionError as exc:
            return {"ok": False, "error": "permission_denied", "detail": str(exc)}
        except OSError as exc:
            return {"ok": False, "error": "write_failed", "detail": str(exc)}

        try:
            stat = target.stat()
            size = stat.st_size
            mtime_ms = int(stat.st_mtime * 1000)
        except OSError:
            size = len(content.encode("utf-8"))
            mtime_ms = int(time.time() * 1000)

        return {
            "ok": True,
            "path": rel,
            "size": size,
            "mtime_ms": mtime_ms,
        }

    def files_delete(client, payload):
        body = payload or {}
        raw = str(body.get("path") or "")
        recursive = bool(body.get("recursive"))
        if not raw:
            return {"ok": False, "error": "missing_path"}

        try:
            target = _resolve(client, raw, must_exist=True)
        except FileNotFoundError as exc:
            return {"ok": False, "error": "not_found", "detail": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__, "detail": str(exc)}

        rel = _rel(client, target)
        if rel in ("", "."):
            return {"ok": False, "error": "refuse_root"}
        if _is_sensitive(rel):
            return {"ok": False, "error": "sensitive_path", "detail": rel}

        try:
            if target.is_dir():
                if recursive:
                    shutil.rmtree(target)
                else:
                    if any(target.iterdir()):
                        return {"ok": False, "error": "not_empty", "detail": rel}
                    target.rmdir()
            else:
                target.unlink()
        except PermissionError as exc:
            return {"ok": False, "error": "permission_denied", "detail": str(exc)}
        except OSError as exc:
            return {"ok": False, "error": "delete_failed", "detail": str(exc)}

        return {"ok": True, "path": rel}

    def files_create(client, payload):
        body = payload or {}
        raw = str(body.get("path") or "")
        kind = str(body.get("kind") or "file").lower().strip()
        if kind not in {"file", "dir"}:
            return {"ok": False, "error": "invalid_kind"}
        if not raw:
            return {"ok": False, "error": "missing_path"}

        try:
            target = _resolve(client, raw)
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__, "detail": str(exc)}

        rel = _rel(client, target)
        if _is_sensitive(rel):
            return {"ok": False, "error": "sensitive_path", "detail": rel}

        if target.exists():
            return {"ok": False, "error": "already_exists", "detail": rel}

        try:
            if kind == "dir":
                target.mkdir(parents=True, exist_ok=False)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.touch(exist_ok=False)
        except PermissionError as exc:
            return {"ok": False, "error": "permission_denied", "detail": str(exc)}
        except OSError as exc:
            return {"ok": False, "error": "create_failed", "detail": str(exc)}

        return {"ok": True, "path": rel, "kind": kind}

    def files_rename(client, payload):
        body = payload or {}
        raw_from = str(body.get("from") or "")
        raw_to = str(body.get("to") or "")
        if not raw_from or not raw_to:
            return {"ok": False, "error": "missing_path"}

        try:
            src = _resolve(client, raw_from, must_exist=True)
            dst = _resolve(client, raw_to)
        except FileNotFoundError as exc:
            return {"ok": False, "error": "not_found", "detail": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__, "detail": str(exc)}

        rel_from = _rel(client, src)
        rel_to = _rel(client, dst)
        if _is_sensitive(rel_from) or _is_sensitive(rel_to):
            return {"ok": False, "error": "sensitive_path", "detail": rel_to}
        if dst.exists():
            return {"ok": False, "error": "already_exists", "detail": rel_to}

        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            src.rename(dst)
        except OSError as exc:
            return {"ok": False, "error": "rename_failed", "detail": str(exc)}

        return {"ok": True, "from": rel_from, "to": rel_to}

    return [
        ("GET", "/workspace", workspace_info),
        ("GET", "/workspace/files", files_list),
        ("GET", "/workspace/file", files_read),
        ("POST", "/workspace/file/save", files_write),
        ("POST", "/workspace/file/delete", files_delete),
        ("POST", "/workspace/file/create", files_create),
        ("POST", "/workspace/file/rename", files_rename),
    ]
